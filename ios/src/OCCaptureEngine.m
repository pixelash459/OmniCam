//
//  OCCaptureEngine.m — one session, both inputs pre-created, instant switch.
//
#import "OCCaptureEngine.h"

@interface OCCaptureEngine () <AVCaptureVideoDataOutputSampleBufferDelegate>
@property (nonatomic, strong) AVCaptureSession *session;
@property (nonatomic, strong, nullable) AVCaptureDeviceInput *backInput;
@property (nonatomic, strong, nullable) AVCaptureDeviceInput *frontInput;
@property (nonatomic, strong) AVCaptureVideoDataOutput *videoOutput;
@property (nonatomic, copy) NSString *activeCameraId;
@property (nonatomic, assign) BOOL wantsHighResolution;
@property (nonatomic, assign) CGFloat zoomFactor;
@property (nonatomic, assign) BOOL torchOn;
@property (nonatomic, strong) dispatch_queue_t sessionQueue; // all configuration serialized here
@property (nonatomic, strong) dispatch_queue_t videoQueue;   // sample-buffer delegate queue
@property (nonatomic, assign) BOOL reconfiguring;            // drop frames during input swap
@end

@implementation OCCaptureEngine

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
    NSLog(@"[OmniCam] capture started (preset %@, camera %@)", _session.sessionPreset, _activeCameraId);
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
    if ([device isExposureModeSupported:AVCaptureExposureModeContinuousAutoExposure]) {
        device.exposureMode = AVCaptureExposureModeContinuousAutoExposure;
    }
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

- (CGFloat)zoomFactor { return _zoomFactor; }
- (NSString *)activeCameraId { return _activeCameraId; }
- (BOOL)wantsHighResolution { return _wantsHighResolution; }
- (BOOL)torchSupported {
    AVCaptureDeviceInput *input = [_activeCameraId isEqualToString:@"front"] ? _frontInput : _backInput;
    return input.device.hasTorch;
}
- (BOOL)isTorchOn { return _torchOn; }

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
    // Called on _videoQueue; the delegate (filter pipeline) does its own retain + async hop.
    [d captureEngine:self didOutputPixelBuffer:pb timestamp:ts];
}

@end
