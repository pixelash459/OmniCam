//
//  OCCaptureEngine.m — one session, both inputs pre-created, instant switch.
//
#import "OCCaptureEngine.h"
#import <QuartzCore/QuartzCore.h> // CACurrentMediaTime

@interface OCCaptureEngine () <AVCaptureVideoDataOutputSampleBufferDelegate>
@property (nonatomic, strong) AVCaptureSession *session;
@property (nonatomic, strong, nullable) AVCaptureDeviceInput *backInput;
@property (nonatomic, strong, nullable) AVCaptureDeviceInput *frontInput;
@property (nonatomic, strong) AVCaptureVideoDataOutput *videoOutput;
@property (nonatomic, copy) NSString *activeCameraId;
@property (nonatomic, assign) BOOL wantsHighResolution;
@property (nonatomic, assign) CGFloat zoomFactor;
@property (nonatomic, assign) BOOL torchOn;
@property (nonatomic, assign, getter=isFocusLocked) BOOL focusLocked;
@property (nonatomic, strong) dispatch_queue_t sessionQueue; // all configuration serialized here
@property (nonatomic, strong) dispatch_queue_t videoQueue;   // sample-buffer delegate queue
@property (nonatomic, assign) BOOL reconfiguring;            // drop frames during input swap
@end

static NSString * const OCScreenModeDefaultsKey = @"omnicam.screenmode.v1";

// Screen-mode metering. Luma is video range (16..235); the "highlight" is the
// 99th percentile of a sparse sample grid, i.e. the screen when one is in shot.
static const double OCScreenTarget = 0.82;      // highlight level to hold (0..1, gamma)
static const double OCScreenShutterS = 1.0 / 60.0; // preferred: averages 60/120 Hz refresh + PWM
static const double OCScreenMaxShutterS = 1.0 / 30.0;
static const float OCScreenMaxISO = 640.0f;     // beyond this the hands are mostly noise

// State for the screen-mode loop. Written on the video queue (metering) and
// the session queue (applying); plain scalars, last-writer-wins is fine.
typedef struct {
    BOOL on;
    double ev;
    BOOL pending;    // a custom-exposure request has not reached frames yet
    int settle;      // frames to skip after it has
    BOOL needsSeed;  // read the device's current AE result before the first step
    unsigned frame;
    double duration; // seconds, last applied
    float iso;
    double hi;       // last highlight level
    double lastLogS;
} OCScreenAE;

@implementation OCCaptureEngine {
    OCScreenAE _sae;
}

// The setters below are implemented by hand, which disables clang's automatic
// ivar synthesis for their properties — synthesize explicitly.
@synthesize zoomFactor = _zoomFactor;
@synthesize wantsHighResolution = _wantsHighResolution;

- (instancetype)init {
    self = [super init];
    if (!self) return nil;

    _session = [[AVCaptureSession alloc] init];
    _sessionQueue = dispatch_queue_create("oc.capture.session", DISPATCH_QUEUE_SERIAL);
    _videoQueue = dispatch_queue_create("oc.capture.video", DISPATCH_QUEUE_SERIAL);
    _activeCameraId = @"back";
    _zoomFactor = 1.0;
    NSDictionary *sm = [[NSUserDefaults standardUserDefaults] dictionaryForKey:OCScreenModeDefaultsKey];
    _sae.on = [sm[@"on"] boolValue];
    _sae.ev = MIN(MAX([sm[@"ev"] doubleValue], -2.0), 2.0);
    _sae.needsSeed = YES;

    // Both inputs pre-created at init so switching never touches the device HAL twice.
    AVCaptureDevice *back = [self deviceAtPosition:AVCaptureDevicePositionBack];
    AVCaptureDevice *front = [self deviceAtPosition:AVCaptureDevicePositionFront];
    NSError *err = nil;
    if (back) _backInput = [AVCaptureDeviceInput deviceInputWithDevice:back error:&err];
    if (front) _frontInput = [AVCaptureDeviceInput deviceInputWithDevice:front error:nil];
    if (!_backInput && !_frontInput) {
        NSLog(@"[OmniCam] capture: no camera available: %@", err);
    }

    // NV12 (420v) end-to-end: matches encoder input AND Core Image render targets,
    // avoiding any pixel-format conversion on the A8 (DECISIONS.md).
    _videoOutput = [[AVCaptureVideoDataOutput alloc] init];
    _videoOutput.videoSettings = @{ (id)kCVPixelBufferPixelFormatTypeKey :
                                    @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) };
    _videoOutput.alwaysDiscardsLateVideoFrames = YES; // real-time streaming over completeness
    [_videoOutput setSampleBufferDelegate:self queue:_videoQueue];

    NSNotificationCenter *nc = [NSNotificationCenter defaultCenter];
    [nc addObserver:self selector:@selector(sessionRuntimeError:)
               name:AVCaptureSessionRuntimeErrorNotification object:_session];
    [nc addObserver:self selector:@selector(sessionInterruptionEnded:)
               name:AVCaptureSessionInterruptionEndedNotification object:_session];
    return self;
}

- (void)dealloc {
    [[NSNotificationCenter defaultCenter] removeObserver:self];
}

- (void)sessionRuntimeError:(NSNotification *)note {
    NSError *err = note.userInfo[AVCaptureSessionErrorKey];
    NSLog(@"[OmniCam] AVCapture runtime error: %@", err);
    dispatch_async(_sessionQueue, ^{
        if (!self->_session.running) {
            [self startLocked:NULL];
        }
    });
    void (^handler)(NSString *) = self.errorHandler;
    if (handler) {
        NSString *msg = err.localizedDescription ?: @"camera recovered";
        dispatch_async(dispatch_get_main_queue(), ^{ handler(msg); });
    }
}

- (void)sessionInterruptionEnded:(NSNotification *)note {
    (void)note;
    dispatch_async(_sessionQueue, ^{
        if (!self->_session.running) {
            [self startLocked:NULL];
        }
    });
}

- (AVCaptureDevice *)deviceAtPosition:(AVCaptureDevicePosition)position {
    AVCaptureDeviceDiscoverySession *s = [AVCaptureDeviceDiscoverySession
        discoverySessionWithDeviceTypes:@[AVCaptureDeviceTypeBuiltInWideAngleCamera]
        mediaType:AVMediaTypeVideo position:position];
    return s.devices.firstObject;
}

- (nullable AVCaptureDeviceInput *)activeInput {
    return [_activeCameraId isEqualToString:@"front"] ? _frontInput : _backInput;
}

#pragma mark Session lifecycle

- (BOOL)startAndReturnError:(NSError **)error {
    __block BOOL ok = NO;
    __block NSError *blockErr = nil;
    dispatch_sync(_sessionQueue, ^{
        ok = [self startLocked:&blockErr];
    });
    if (error) *error = blockErr;
    return ok;
}

- (BOOL)startLocked:(NSError **)error {
    if (_session.running) return YES;

    AVAuthorizationStatus st = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    if (st == AVAuthorizationStatusNotDetermined) {
        dispatch_semaphore_t sem = dispatch_semaphore_create(0);
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) {
            dispatch_semaphore_signal(sem);
        }];
        dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(10 * NSEC_PER_SEC)));
    }
    if ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo] != AVAuthorizationStatusAuthorized) {
        if (error) {
            *error = [NSError errorWithDomain:@"OCCaptureEngine" code:1
                          userInfo:@{NSLocalizedDescriptionKey : @"Camera permission denied"}];
        }
        return NO;
    }

    AVCaptureDeviceInput *input = [self activeInput];
    if (!input) {
        if (error) {
            *error = [NSError errorWithDomain:@"OCCaptureEngine" code:2
                          userInfo:@{NSLocalizedDescriptionKey : @"No camera device available"}];
        }
        return NO;
    }

    [_session beginConfiguration];
    NSString *preset = [self desiredPreset];
    if ([_session canSetSessionPreset:preset]) {
        _session.sessionPreset = preset;
    } else if ([_session canSetSessionPreset:AVCaptureSessionPreset1280x720]) {
        _session.sessionPreset = AVCaptureSessionPreset1280x720;
    }
    if ([_session canAddInput:input]) [_session addInput:input];
    if ([_session canAddOutput:_videoOutput]) [_session addOutput:_videoOutput];
    [self applyConnectionSettingsLocked];
    [self configureDeviceLocked:input.device];
    [_session commitConfiguration];

    [_session startRunning]; // blocking by design; runs on sessionQueue
    AVCaptureDeviceFormat *fmt = input.device.activeFormat;
    NSLog(@"[OmniCam] capture started (preset %@, camera %@; ISO %.0f-%.0f, shutter 1/%.0f-1/%.0f s, videoHDR %@, screen mode %@)",
          _session.sessionPreset, _activeCameraId, fmt.minISO, fmt.maxISO,
          1.0 / CMTimeGetSeconds(fmt.maxExposureDuration), 1.0 / CMTimeGetSeconds(fmt.minExposureDuration),
          fmt.videoHDRSupported ? @"yes" : @"no", _sae.on ? @"on" : @"off");
    return YES;
}

- (void)stop {
    dispatch_async(_sessionQueue, ^{
        [self->_session stopRunning];
    });
}

- (void)ensureRunning {
    dispatch_async(_sessionQueue, ^{
        if (!self->_session.running) {
            [self startLocked:NULL];
        }
    });
}

- (NSString *)desiredPreset {
    if (_wantsHighResolution && [_activeCameraId isEqualToString:@"back"]) {
        return AVCaptureSessionPreset1920x1080;
    }
    return AVCaptureSessionPreset1280x720;
}

- (BOOL)isRunning {
    return _session.isRunning;
}

- (void)setWantsHighResolution:(BOOL)hd {
    [self setWantsHighResolution:hd completion:nil];
}

- (void)setWantsHighResolution:(BOOL)hd completion:(void (^)(void))completion {
    self->_wantsHighResolution = hd;
    dispatch_async(_sessionQueue, ^{
        if (self->_session.running && [self->_activeCameraId isEqualToString:@"back"]) {
            NSString *preset = [self desiredPreset];
            if ([self->_session canSetSessionPreset:preset]
                && ![self->_session.sessionPreset isEqualToString:preset]) {
                self->_reconfiguring = YES;
                @try {
                    [self->_session beginConfiguration];
                    self->_session.sessionPreset = preset;
                    [self->_session commitConfiguration];
                    NSLog(@"[OmniCam] capture preset -> %@", preset);
                    if (self->_sae.on) { // new format: HDR and our exposure start over
                        AVCaptureDevice *dev = [self activeInput].device;
                        if ([dev lockForConfiguration:nil]) {
                            [self applyVideoHDRLocked:dev on:YES];
                            [dev unlockForConfiguration];
                        }
                        self->_sae.needsSeed = YES;
                    }
                } @catch (NSException *ex) {
                    NSLog(@"[OmniCam] capture preset exception: %@", ex);
                    @try { [self->_session commitConfiguration]; } @catch (NSException *ex2) {}
                } @finally {
                    self->_reconfiguring = NO;
                }
            }
        }
        void (^cb)(void) = completion;
        if (cb) {
            dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), cb);
        }
    });
}

#pragma mark Connection / device configuration

// Fixed-landscape stream: orientation locked to LandscapeRight permanently (the PC
// expects a stable landscape 16:9 feed regardless of how the phone is held).
- (void)applyConnectionSettingsLocked {
    AVCaptureConnection *conn = [_videoOutput.connections firstObject];
    if (!conn) return;
    if (conn.supportsVideoOrientation) {
        conn.videoOrientation = AVCaptureVideoOrientationLandscapeRight;
    }
    if (conn.supportsVideoMirroring) {
        conn.videoMirrored = [_activeCameraId isEqualToString:@"front"];
    }
}

- (void)configureDeviceLocked:(AVCaptureDevice *)device {
    if (!device) return;
    NSError *err = nil;
    if (![device lockForConfiguration:&err]) {
        NSLog(@"[OmniCam] lockForConfiguration failed: %@", err);
        return;
    }
    if ([device isFocusModeSupported:AVCaptureFocusModeContinuousAutoFocus]) {
        device.focusMode = AVCaptureFocusModeContinuousAutoFocus;
    }
    _focusLocked = NO; // new device starts on continuous AF/AE
    if ([device isExposureModeSupported:AVCaptureExposureModeContinuousAutoExposure]) {
        device.exposureMode = AVCaptureExposureModeContinuousAutoExposure;
    }
    [self applyVideoHDRLocked:device on:_sae.on];
    // Screen mode re-seeds from this device's own AE result once it settles.
    _sae.needsSeed = YES;
    _sae.pending = NO;
    _sae.settle = 8;
    CGFloat maxZ = MIN(device.activeFormat.videoMaxZoomFactor, 16.0);
    device.videoZoomFactor = MIN(MAX(_zoomFactor, 1.0), maxZ);
    if (device.hasTorch && device.torchAvailable) {
        device.torchMode = _torchOn ? AVCaptureTorchModeOn : AVCaptureTorchModeOff;
    }
    [device unlockForConfiguration];
}

#pragma mark Camera switch (single-session, instant)

- (void)switchToCameraId:(NSString *)cameraId
              completion:(void (^)(NSString *, NSError *))completion {
    dispatch_async(_sessionQueue, ^{
        NSError *err = nil;
        if ([cameraId isEqualToString:self->_activeCameraId]) {
            if (completion) {
                NSString *active = self->_activeCameraId;
                dispatch_async(dispatch_get_main_queue(), ^{ completion(active, nil); });
            }
            return;
        }
        AVCaptureDeviceInput *target = [cameraId isEqualToString:@"front"] ? self->_frontInput : self->_backInput;
        AVCaptureDeviceInput *current = [self activeInput];
        if (!target) {
            err = [NSError errorWithDomain:@"OCCaptureEngine" code:3
                          userInfo:@{NSLocalizedDescriptionKey : @"Requested camera unavailable"}];
        } else if (!self->_session.running) {
            self->_activeCameraId = [cameraId isEqualToString:@"front"] ? @"front" : @"back";
        } else {
            // A8 front camera cannot run 1080p. Swapping inputs while the session
            // is still on the 1080p preset throws / runtime-errors the session
            // (HUD connect/disconnect strobe as the app recovers). Drop preset
            // to 720p *before* removing the back camera.
            self->_reconfiguring = YES;
            BOOL began = NO;
            @try {
                BOOL wantFront = [cameraId isEqualToString:@"front"];
                if (wantFront) self->_wantsHighResolution = NO;
                NSString *oldId = self->_activeCameraId;
                self->_activeCameraId = wantFront ? @"front" : @"back";
                [self->_session beginConfiguration];
                began = YES;
                NSString *preset = [self desiredPreset];
                if ([self->_session canSetSessionPreset:preset]) {
                    self->_session.sessionPreset = preset;
                }
                if (current) [self->_session removeInput:current];
                if ([self->_session canAddInput:target]) {
                    [self->_session addInput:target];
                    [self applyConnectionSettingsLocked];
                    [self configureDeviceLocked:target.device];
                } else {
                    self->_activeCameraId = oldId;
                    if (current && [self->_session canAddInput:current]) {
                        [self->_session addInput:current];
                    }
                    NSString *fallback = [self desiredPreset];
                    if ([self->_session canSetSessionPreset:fallback]) {
                        self->_session.sessionPreset = fallback;
                    }
                    [self applyConnectionSettingsLocked];
                    err = [NSError errorWithDomain:@"OCCaptureEngine" code:4
                                  userInfo:@{NSLocalizedDescriptionKey : @"Cannot add requested camera input"}];
                }
                [self->_session commitConfiguration];
                began = NO;
                if (!err && wantFront) self->_torchOn = NO;
            } @catch (NSException *ex) {
                NSLog(@"[OmniCam] camera switch exception: %@", ex);
                err = [NSError errorWithDomain:@"OCCaptureEngine" code:5
                              userInfo:@{NSLocalizedDescriptionKey : ex.reason ?: @"camera switch failed"}];
                if (began) {
                    @try { [self->_session commitConfiguration]; } @catch (NSException *ex2) {
                        NSLog(@"[OmniCam] camera switch commit after exception: %@", ex2);
                    }
                }
            }
            self->_reconfiguring = NO;
            if (!self->_session.running) {
                [self startLocked:NULL];
            }
        }
        NSLog(@"[OmniCam] camera switch -> %@ err: %@", self->_activeCameraId, err);
        if (completion) {
            NSString *active = self->_activeCameraId;
            NSError *doneErr = err;
            dispatch_async(dispatch_get_main_queue(), ^{ completion(active, doneErr); });
        }
    });
}

#pragma mark Torch / zoom

- (BOOL)applyTorchOn:(BOOL)on {
    __block BOOL ok = NO;
    dispatch_sync(_sessionQueue, ^{
        AVCaptureDeviceInput *input = [self activeInput];
        AVCaptureDevice *dev = input.device;
        if (!dev.hasTorch || !dev.torchAvailable) {
            self->_torchOn = NO;
            ok = NO;
            return;
        }
        NSError *err = nil;
        if ([dev lockForConfiguration:&err]) {
            dev.torchMode = on ? AVCaptureTorchModeOn : AVCaptureTorchModeOff;
            [dev unlockForConfiguration];
            self->_torchOn = on;
            ok = on;
        }
    });
    return ok;
}

- (void)setZoomFactor:(CGFloat)factor {
    dispatch_sync(_sessionQueue, ^{
        AVCaptureDeviceInput *input = [self activeInput];
        AVCaptureDevice *dev = input.device;
        CGFloat maxZ = MIN(dev.activeFormat.videoMaxZoomFactor, 16.0);
        self->_zoomFactor = MIN(MAX(factor, 1.0), maxZ);
        NSError *err = nil;
        if ([dev lockForConfiguration:&err]) {
            dev.videoZoomFactor = self->_zoomFactor;
            [dev unlockForConfiguration];
        }
    });
}

- (CGFloat)maxZoomFactor {
    __block CGFloat z = 1.0;
    dispatch_sync(_sessionQueue, ^{
        AVCaptureDeviceInput *input = [self activeInput];
        z = MIN(input.device.activeFormat.videoMaxZoomFactor, 16.0);
    });
    return z;
}

#pragma mark Focus / exposure

- (void)focusAtDevicePoint:(CGPoint)point lock:(BOOL)lock {
    CGPoint p = CGPointMake(MIN(MAX(point.x, 0.0), 1.0), MIN(MAX(point.y, 0.0), 1.0));
    dispatch_async(_sessionQueue, ^{
        AVCaptureDevice *dev = [self activeInput].device;
        NSError *err = nil;
        if (!dev || ![dev lockForConfiguration:&err]) {
            NSLog(@"[OmniCam] focus: lockForConfiguration failed: %@", err);
            return;
        }
        AVCaptureFocusMode fm = lock ? AVCaptureFocusModeAutoFocus : AVCaptureFocusModeContinuousAutoFocus;
        if (dev.focusPointOfInterestSupported && [dev isFocusModeSupported:fm]) {
            dev.focusPointOfInterest = p;
            dev.focusMode = fm; // AutoFocus = focus once there, then hold
        }
        AVCaptureExposureMode em = lock ? AVCaptureExposureModeAutoExpose : AVCaptureExposureModeContinuousAutoExposure;
        // Screen mode owns exposure; a tap there only focuses.
        if (!self->_sae.on && dev.exposurePointOfInterestSupported && [dev isExposureModeSupported:em]) {
            dev.exposurePointOfInterest = p;
            dev.exposureMode = em;
        }
        // Unlocked taps keep tracking the subject; locked ones must not drift.
        dev.subjectAreaChangeMonitoringEnabled = NO;
        [dev unlockForConfiguration];
        self->_focusLocked = lock;
        NSLog(@"[OmniCam] focus %@ at (%.2f, %.2f)", lock ? @"locked" : @"set", p.x, p.y);
    });
}

- (void)focusAtStreamPoint:(CGPoint)point lock:(BOOL)lock {
    // The stream is LandscapeRight, which is the device POI frame; the front
    // camera's stream is mirrored, so flip x back.
    if ([_activeCameraId isEqualToString:@"front"]) point.x = 1.0 - point.x;
    [self focusAtDevicePoint:point lock:lock];
}

- (void)resumeContinuousFocus {
    dispatch_async(_sessionQueue, ^{
        AVCaptureDevice *dev = [self activeInput].device;
        if (!dev || ![dev lockForConfiguration:nil]) return;
        CGPoint centre = CGPointMake(0.5, 0.5);
        if (dev.focusPointOfInterestSupported) dev.focusPointOfInterest = centre;
        if ([dev isFocusModeSupported:AVCaptureFocusModeContinuousAutoFocus]) {
            dev.focusMode = AVCaptureFocusModeContinuousAutoFocus;
        }
        if (!self->_sae.on) {
            if (dev.exposurePointOfInterestSupported) dev.exposurePointOfInterest = centre;
            if ([dev isExposureModeSupported:AVCaptureExposureModeContinuousAutoExposure]) {
                dev.exposureMode = AVCaptureExposureModeContinuousAutoExposure;
            }
        }
        [dev unlockForConfiguration];
        self->_focusLocked = NO;
        NSLog(@"[OmniCam] focus back to continuous");
    });
}

- (CGFloat)zoomFactor { return _zoomFactor; }
- (NSString *)activeCameraId { return _activeCameraId; }
- (BOOL)wantsHighResolution { return _wantsHighResolution; }
- (BOOL)torchSupported {
    AVCaptureDeviceInput *input = [_activeCameraId isEqualToString:@"front"] ? _frontInput : _backInput;
    return input.device.hasTorch;
}
- (BOOL)isTorchOn { return _torchOn; }

#pragma mark Screen mode (handcam exposure)

- (BOOL)isScreenMode { return _sae.on; }
- (double)screenEV { return _sae.ev; }

- (void)setScreenMode:(BOOL)on ev:(double)ev {
    ev = isfinite(ev) ? MIN(MAX(ev, -2.0), 2.0) : 0.0;
    // Flags flip now so the session echo reports them; device work is queued.
    BOOL was = _sae.on;
    _sae.ev = ev;
    _sae.on = on;
    [[NSUserDefaults standardUserDefaults] setObject:@{@"on" : @(on), @"ev" : @(ev)}
                                              forKey:OCScreenModeDefaultsKey];
    if (on == was) return;
    dispatch_async(_sessionQueue, ^{
        AVCaptureDevice *dev = [self activeInput].device;
        if (dev && [dev lockForConfiguration:nil]) {
            [self applyVideoHDRLocked:dev on:on];
            if (!on && [dev isExposureModeSupported:AVCaptureExposureModeContinuousAutoExposure]) {
                dev.exposureMode = AVCaptureExposureModeContinuousAutoExposure;
            }
            [dev unlockForConfiguration];
        }
        if (on) {
            self->_sae.needsSeed = YES;
            self->_sae.pending = NO;
            self->_sae.settle = 2; // let the HDR switch reach the frames first
        }
        NSLog(@"[OmniCam] screen mode %@ (ev %+.1f)", on ? @"on" : @"off", ev);
    });
}

// Device must be locked. The sensor's video HDR (720p/1080p on the A8 back
// camera) widens the range the frame can hold, which is exactly the screen vs.
// hands problem; outside screen mode the system decides as before.
- (void)applyVideoHDRLocked:(AVCaptureDevice *)dev on:(BOOL)on {
    if (!dev.activeFormat.videoHDRSupported) return;
    @try {
        if (on) {
            dev.automaticallyAdjustsVideoHDREnabled = NO;
            dev.videoHDREnabled = YES;
        } else {
            dev.automaticallyAdjustsVideoHDREnabled = YES;
        }
    } @catch (NSException *ex) {
        NSLog(@"[OmniCam] video HDR %@ failed: %@", on ? @"on" : @"off", ex);
    }
}

- (NSDictionary *)exposureInfo {
    if (!_sae.on || _sae.duration <= 0) return @{};
    return @{@"exp_ms" : @(round(_sae.duration * 10000.0) / 10.0),
             @"iso" : @(lroundf(_sae.iso)),
             @"hi" : @(round(_sae.hi * 100.0) / 100.0)};
}

// Video queue. Samples every 8th pixel of every 8th row of the luma plane
// (~14k samples at 720p, well under a millisecond on the A8), every 3rd frame.
- (void)meterScreenExposure:(CVPixelBufferRef)pb {
    if (++_sae.frame % 3 != 0) return;
    if (_sae.pending) return;
    if (_sae.settle > 0) { _sae.settle--; return; }
    if (CVPixelBufferGetPlaneCount(pb) < 1) return;
    if (CVPixelBufferLockBaseAddress(pb, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) return;
    const uint8_t *y = CVPixelBufferGetBaseAddressOfPlane(pb, 0);
    size_t stride = CVPixelBufferGetBytesPerRowOfPlane(pb, 0);
    size_t w = CVPixelBufferGetWidthOfPlane(pb, 0), h = CVPixelBufferGetHeightOfPlane(pb, 0);
    uint32_t hist[256] = {0};
    uint32_t n = 0;
    if (y) {
        for (size_t r = 4; r < h; r += 8) {
            const uint8_t *row = y + r * stride;
            for (size_t c = 4; c < w; c += 8) { hist[row[c]]++; n++; }
        }
    }
    CVPixelBufferUnlockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);
    if (n < 100) return;

    uint32_t want = n / 100, acc = 0; // top 1%
    int p99 = 255;
    for (; p99 > 0; p99--) { acc += hist[p99]; if (acc >= want) break; }
    uint32_t clipped = 0;
    for (int i = 233; i < 256; i++) clipped += hist[i];
    double hi = MIN(MAX((p99 - 16) / 219.0, 0.0), 1.0);
    _sae.hi = hi;

    // Error in stops between the highlight and the target (both gamma-encoded,
    // ~2.2). A clipped highlight hides how far over it is: step down hard.
    double targetLin = MIN(pow(OCScreenTarget, 2.2) * pow(2.0, _sae.ev), pow(0.95, 2.2));
    double step;
    if (hi >= 0.97) {
        step = (clipped > n / 50) ? -1.0 : -0.5;
    } else {
        step = log2(targetLin / MAX(pow(hi, 2.2), 1e-4)) * 0.6; // damped
        step = MIN(MAX(step, -1.0), 0.75);
        if (fabs(step) < 0.12) step = 0.0;
    }
    if (step == 0.0 && !_sae.needsSeed) return;
    _sae.pending = YES;
    dispatch_async(_sessionQueue, ^{ [self applyScreenExposureStep:step]; });
}

// Session queue. Turns an exposure change (stops) into shutter + ISO.
- (void)applyScreenExposureStep:(double)step {
    AVCaptureDevice *dev = [self activeInput].device;
    if (!_sae.on || !dev || _reconfiguring
        || ![dev isExposureModeSupported:AVCaptureExposureModeCustom]) {
        _sae.pending = NO;
        return;
    }
    AVCaptureDeviceFormat *fmt = dev.activeFormat;
    double dMin = CMTimeGetSeconds(fmt.minExposureDuration);
    double dMax = MIN(CMTimeGetSeconds(fmt.maxExposureDuration), OCScreenMaxShutterS);
    float isoMin = fmt.minISO, isoMax = MIN(fmt.maxISO, OCScreenMaxISO);
    if (_sae.needsSeed || _sae.duration <= 0 || _sae.iso <= 0) {
        _sae.duration = CMTimeGetSeconds(dev.exposureDuration);
        _sae.iso = dev.ISO;
        _sae.needsSeed = NO;
        if (!(_sae.duration > 0) || !(_sae.iso > 0)) { _sae.duration = OCScreenShutterS; _sae.iso = isoMin; }
    }
    // Total exposure, then split it: shutter at 1/60 s where possible, ISO
    // takes the rest; only once ISO bottoms out does the shutter get shorter.
    double product = _sae.duration * _sae.iso * pow(2.0, step);
    product = MIN(MAX(product, dMin * isoMin), dMax * isoMax);
    double d = MIN(MAX(OCScreenShutterS, dMin), dMax);
    double iso = product / d;
    if (iso < isoMin) { iso = isoMin; d = MAX(product / iso, dMin); }
    else if (iso > isoMax) { iso = isoMax; d = MIN(product / iso, dMax); }

    // Pinned at a limit (dark room at max, or min ISO at the shortest shutter):
    // nothing to change, so do not keep re-sending the same request.
    if (dev.exposureMode == AVCaptureExposureModeCustom
        && fabs(d - _sae.duration) < 1e-6 && fabs(iso - _sae.iso) < 0.5) {
        _sae.pending = NO;
        _sae.settle = 3;
        return;
    }
    NSError *err = nil;
    if (![dev lockForConfiguration:&err]) { _sae.pending = NO; return; }
    _sae.duration = d;
    _sae.iso = (float)iso;
    [dev setExposureModeCustomWithDuration:CMTimeMakeWithSeconds(d, 1000000)
                                       ISO:(float)iso
                         completionHandler:^(CMTime syncTime) {
        (void)syncTime;
        dispatch_async(self->_videoQueue, ^{
            self->_sae.pending = NO;
            self->_sae.settle = 1; // one more metering slot before judging
        });
    }];
    [dev unlockForConfiguration];

    double now = CACurrentMediaTime();
    if (now - _sae.lastLogS > 2.0) {
        _sae.lastLogS = now;
        NSLog(@"[OmniCam] screen AE: hi %.2f step %+.2f -> 1/%.0f s ISO %.0f", _sae.hi, step, 1.0 / d, iso);
    }
}

#pragma mark Frame delivery

- (void)captureOutput:(AVCaptureOutput *)output
didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
       fromConnection:(AVCaptureConnection *)connection {
    id<OCCaptureEngineDelegate> d = _delegate;
    if (!d) return;
    if (self->_reconfiguring) return;
    CVPixelBufferRef pb = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (!pb) return;
    CMTime ts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer);
    if (_sae.on) [self meterScreenExposure:pb];
    // Called on _videoQueue; the delegate (filter pipeline) does its own retain + async hop.
    [d captureEngine:self didOutputPixelBuffer:pb timestamp:ts];
}

@end
