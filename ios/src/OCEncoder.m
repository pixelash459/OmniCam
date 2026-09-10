//
//  OCEncoder.m — VTCompressionSession per PROTOCOL.md §7, AVCC→Annex-B conversion.
//
#import "OCEncoder.h"

#import <stdlib.h>
#import <mach/mach_time.h>

static void ocCheck(OSStatus st, NSString *what) {
    if (st != noErr) NSLog(@"[OmniCam] VTSessionSetProperty %@ failed: %d", what, (int)st);
}

// The iOS 12.4 SDK predates CMVideoFormatDescriptionGet(NumberOf)ParameterSets, so
// extract SPS/PPS from the 'avcC' sample-description atom instead (H.264 config record).
static BOOL OCParseAvcCParameterSets(CMFormatDescriptionRef desc, NSData **outSPS, NSData **outPPS) {
    if (outSPS) *outSPS = nil;
    if (outPPS) *outPPS = nil;
    CFDictionaryRef atoms = (CFDictionaryRef)CMFormatDescriptionGetExtension(
        desc, kCMFormatDescriptionExtension_SampleDescriptionExtensionAtoms);
    if (!atoms || CFGetTypeID(atoms) != CFDictionaryGetTypeID()) return NO;
    CFDataRef avcC = (CFDataRef)CFDictionaryGetValue(atoms, CFSTR("avcC"));
    if (!avcC || CFGetTypeID(avcC) != CFDataGetTypeID()) return NO;
    const uint8_t *b = CFDataGetBytePtr(avcC);
    CFIndex len = CFDataGetLength(avcC);
    if (!b || len < 7) return NO;
    // avcC: version, profile, compat, level, 0xFC|lengthSizeMinusOne, 0xE0|numSPS, [len16 sps]…, numPPS, [len16 pps]…
    NSUInteger off = 6;
    NSData *sps = nil;
    unsigned numSPS = b[5] & 0x1F;
    for (unsigned i = 0; i < numSPS; i++) {
        if (off + 2 > (NSUInteger)len) return NO;
        NSUInteger l = ((NSUInteger)b[off] << 8) | b[off + 1];
        off += 2;
        if (off + l > (NSUInteger)len) return NO;
        if (!sps && l > 0) sps = [NSData dataWithBytes:b + off length:l];
        off += l;
    }
    if (off + 1 > (NSUInteger)len) return NO;
    unsigned numPPS = b[off] & 0x1F;
    off += 1;
    NSData *pps = nil;
    for (unsigned i = 0; i < numPPS; i++) {
        if (off + 2 > (NSUInteger)len) return NO;
        NSUInteger l = ((NSUInteger)b[off] << 8) | b[off + 1];
        off += 2;
        if (off + l > (NSUInteger)len) return NO;
        if (!pps && l > 0) pps = [NSData dataWithBytes:b + off length:l];
        off += l;
    }
    if (!sps || !pps) return NO;
    if (outSPS) *outSPS = sps;
    if (outPPS) *outPPS = pps;
    return YES;
}

@interface OCEncoder ()
@property (nonatomic, strong) dispatch_queue_t vtQueue;   // session create/destroy/encode
@property (nonatomic, strong) dispatch_queue_t cbQueue;   // output callbacks → delegate
@property (nonatomic, assign, nullable) VTCompressionSessionRef session;
@property (nonatomic, assign, nullable) CVPixelBufferPoolRef pool; // +1 mirror of the session pool
@property (nonatomic, assign) int sessionWidth;
@property (nonatomic, assign) int sessionHeight;
@property (nonatomic, assign) int fps;
@property (nonatomic, assign) int keyint;
@property (nonatomic, assign) int bitrateKbps;
@property (nonatomic, assign) BOOL running;
@property (nonatomic, assign) BOOL forceKeyNext;
@property (nonatomic, copy, nullable) NSData *cachedSPS;
@property (nonatomic, copy, nullable) NSData *cachedPPS;
@property (nonatomic, assign) double lastEncodeDurationMs;
@end

static void OCEncodeOutputCallback(void *outputCallbackRefCon,
                                   void *sourceFrameRefCon,
                                   OSStatus status,
                                   VTEncodeInfoFlags infoFlags,
                                   CMSampleBufferRef sampleBuffer);

@implementation OCEncoder

- (instancetype)init {
    self = [super init];
    if (!self) return nil;
    _vtQueue = dispatch_queue_create("oc.encoder.vt", DISPATCH_QUEUE_SERIAL);
    _cbQueue = dispatch_queue_create("oc.encoder.cb", DISPATCH_QUEUE_SERIAL);
    _fps = 30;
    _keyint = 60;
    _bitrateKbps = 3000;
    return self;
}

- (void)dealloc {
    _running = NO;
    // Must CompleteFrames + Invalidate on _vtQueue. Doing it here (any thread)
    // while EncodeFrame was in flight crashed iOS 12 / hung process-exit (0x8badf00d).
    if (_vtQueue) {
        dispatch_sync(_vtQueue, ^{
            [self stopLocked];
        });
    }
}

- (BOOL)isRunning { return _running; }
- (double)lastEncodeDurationMs { return _lastEncodeDurationMs; }

#pragma mark Session lifecycle

- (BOOL)startWithWidth:(int)width
                height:(int)height
                   fps:(int)fps
           bitrateKbps:(int)kbps
                keyint:(int)keyint
                 error:(NSError **)error {
    __block BOOL ok = NO;
    __block NSError *blockErr = nil;
    dispatch_sync(_vtQueue, ^{
        ok = [self startLocked:width height:height fps:fps kbps:kbps keyint:keyint error:&blockErr];
    });
    if (error) *error = blockErr;
    return ok;
}

- (BOOL)startLocked:(int)width
             height:(int)height
                fps:(int)fps
               kbps:(int)kbps
             keyint:(int)keyint
              error:(NSError **)error {
    [self stopLocked];

    // Hardware encode is the default (and effectively only) path on iOS — the
    // opt-in specification keys are macOS-only in the iOS 12.4 SDK headers
    // (declared under #if !TARGET_OS_IPHONE), so pass no encoder specification.
    // Pool buffers are handed to Core Image → keep them IOSurface/GLES/Metal compatible.
    NSDictionary *pbAttrs = @{
        (id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
        (id)kCVPixelBufferOpenGLESCompatibilityKey : @YES,
        (id)kCVPixelBufferMetalCompatibilityKey : @YES,
    };

    VTCompressionSessionRef session = NULL;
    OSStatus st = VTCompressionSessionCreate(kCFAllocatorDefault,
                                             (int32_t)width, (int32_t)height,
                                             kCMVideoCodecType_H264,
                                             NULL,
                                             (__bridge CFDictionaryRef)pbAttrs,
                                             NULL,
                                             OCEncodeOutputCallback,
                                             (__bridge void *)self,
                                             &session);
    if (st != noErr || !session) {
        NSLog(@"[OmniCam] VTCompressionSessionCreate failed: %d (%dx%d@%d, %d kbps)", (int)st, width, height, fps, kbps);
        if (error) {
            *error = [NSError errorWithDomain:NSOSStatusErrorDomain code:st
                          userInfo:@{NSLocalizedDescriptionKey :
                                     [NSString stringWithFormat:@"encoder create failed (%d)", (int)st]}];
        }
        return NO;
    }
    _session = session;

    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_RealTime, kCFBooleanTrue), @"RealTime");
    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse), @"AllowFrameReordering");
    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_ProfileLevel,
                                 kVTProfileLevel_H264_Main_AutoLevel), @"ProfileLevel");
    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_AverageBitRate,
                                 (__bridge CFTypeRef)@((int32_t)kbps * 1000)), @"AverageBitRate");
    // §7: DataRateLimits = [bytes, 1 second window].
    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_DataRateLimits,
                                 (__bridge CFTypeRef)@[@((int32_t)(kbps * 1000 / 8)), @1]), @"DataRateLimits");
    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_MaxKeyFrameInterval,
                                 (__bridge CFTypeRef)@((int32_t)(keyint > 0 ? keyint : 60))), @"MaxKeyFrameInterval");
    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_ExpectedFrameRate,
                                 (__bridge CFTypeRef)@((int32_t)(fps > 0 ? fps : 30))), @"ExpectedFrameRate");
    ocCheck(VTSessionSetProperty(session, kVTCompressionPropertyKey_MaxFrameDelayCount,
                                 (__bridge CFTypeRef)@1), @"MaxFrameDelayCount");

    st = VTCompressionSessionPrepareToEncodeFrames(session);
    if (st != noErr) {
        NSLog(@"[OmniCam] VTCompressionSessionPrepareToEncodeFrames failed: %d", (int)st);
        VTCompressionSessionInvalidate(session);
        CFRelease(session);
        _session = NULL;
        if (error) {
            *error = [NSError errorWithDomain:NSOSStatusErrorDomain code:st
                          userInfo:@{NSLocalizedDescriptionKey :
                                     [NSString stringWithFormat:@"encoder prepare failed (%d)", (int)st]}];
        }
        return NO;
    }

    _sessionWidth = width;
    _sessionHeight = height;
    _fps = fps > 0 ? fps : 30;
    _keyint = keyint > 0 ? keyint : 60;
    _bitrateKbps = kbps;
    _forceKeyNext = YES;
    _cachedSPS = nil;
    _cachedPPS = nil;
    _running = YES;

    if (_pool) {
        CFRelease(_pool);
        _pool = NULL;
    }
    CVPixelBufferPoolRef p = VTCompressionSessionGetPixelBufferPool(session);
    if (p) {
        _pool = p;
        CFRetain(_pool);
    }

    NSLog(@"[OmniCam] encoder started %dx%d@%d %d kbps keyint %d", width, height, _fps, kbps, _keyint);
    return YES;
}

- (void)stop {
    _running = NO;
    dispatch_async(_vtQueue, ^{
        [self stopLocked];
    });
}

- (void)pauseEncoding {
    _running = NO;
    dispatch_async(_vtQueue, ^{
        self->_running = NO;
    });
}

- (void)stopLocked {
    _running = NO;
    if (!_session) return;
    // Drain in-flight encodes before Invalidate — skipping this crashed OmniCam
    // on iOS 12 when the phone STOP button ran while VT still had frames.
    OSStatus st = VTCompressionSessionCompleteFrames(_session, kCMTimeInvalid);
    if (st != noErr) NSLog(@"[OmniCam] CompleteFrames: %d", (int)st);
    VTCompressionSessionInvalidate(_session);
    CFRelease(_session);
    _session = NULL;
    if (_pool) {
        CFRelease(_pool);
        _pool = NULL;
    }
    _cachedSPS = nil;
    _cachedPPS = nil;
    NSLog(@"[OmniCam] encoder stopped");
}

- (nullable CVPixelBufferPoolRef)currentPixelBufferPool CF_RETURNS_RETAINED {
    __block CVPixelBufferPoolRef out = NULL;
    dispatch_sync(_vtQueue, ^{
        if (self->_pool) {
            out = self->_pool;
            CFRetain(out);
        }
    });
    return out;
}

#pragma mark Properties

- (void)setBitrateKbps:(int)kbps {
    if (kbps <= 0) return;
    dispatch_async(_vtQueue, ^{
        self->_bitrateKbps = kbps;
        if (!self->_session) return;
        OSStatus st = VTSessionSetProperty(self->_session, kVTCompressionPropertyKey_AverageBitRate,
                                           (__bridge CFTypeRef)@((int32_t)kbps * 1000));
        ocCheck(st, @"AverageBitRate(live)");
        st = VTSessionSetProperty(self->_session, kVTCompressionPropertyKey_DataRateLimits,
                                  (__bridge CFTypeRef)@[@((int32_t)(kbps * 1000 / 8)), @1]);
        ocCheck(st, @"DataRateLimits(live)");
    });
}

- (void)forceKeyframe {
    dispatch_async(_vtQueue, ^{
        self->_forceKeyNext = YES;
    });
}

#pragma mark Encoding

// OCFilterPipelineDelegate: filtered frames arrive here from the pipeline's render queue.
- (void)filterPipeline:(OCFilterPipeline *)pipeline
 didOutputPixelBuffer:(CVPixelBufferRef)pixelBuffer
             timestamp:(CMTime)timestamp {
    [self encodePixelBuffer:pixelBuffer timestamp:timestamp];
}

- (void)encodePixelBuffer:(CVPixelBufferRef)buffer timestamp:(CMTime)pts {
    if (!_running) return; // stop is in flight / idle preview — don't queue encodes
    CFRetain(buffer); // held across the async hop to _vtQueue
    dispatch_async(_vtQueue, ^{
        [self encodeLocked:buffer timestamp:pts];
        CFRelease(buffer);
    });
}

- (void)encodeLocked:(CVPixelBufferRef)buffer timestamp:(CMTime)pts {
    if (!_session || !_running) return;

    // Resolution/fps changes (camera switch, 720p⇄1080p) arrive as differently-sized
    // buffers; recreate the session so the pool and stream match (PROTOCOL started dims
    // are advisory — actual buffer dims win).
    int w = (int)CVPixelBufferGetWidth(buffer);
    int h = (int)CVPixelBufferGetHeight(buffer);
    if (w != _sessionWidth || h != _sessionHeight) {
        NSLog(@"[OmniCam] encoder input %dx%d != session %dx%d → recreating", w, h, _sessionWidth, _sessionHeight);
        NSError *err = nil;
        if (![self startLocked:w height:h fps:_fps kbps:_bitrateKbps keyint:_keyint error:&err]) {
            NSLog(@"[OmniCam] encoder re-create failed: %@", err);
            return;
        }
    }

    CFMutableDictionaryRef frameProps = NULL;
    if (_forceKeyNext) {
        _forceKeyNext = NO;
        frameProps = CFDictionaryCreateMutable(kCFAllocatorDefault, 1, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
        CFDictionarySetValue(frameProps, kVTEncodeFrameOptionKey_ForceKeyFrame, kCFBooleanTrue);
    }

    uint64_t t0 = mach_absolute_time();
    VTEncodeInfoFlags flags = 0;
    OSStatus st = VTCompressionSessionEncodeFrame(_session, buffer, pts, kCMTimeInvalid,
                                                  frameProps, NULL, &flags);
    if (frameProps) CFRelease(frameProps);
    if (st != noErr) {
        NSLog(@"[OmniCam] EncodeFrame failed: %d", (int)st);
        return;
    }
    static mach_timebase_info_data_t tb;
    if (tb.denom == 0) mach_timebase_info(&tb);
    uint64_t ns = (mach_absolute_time() - t0) * tb.numer / tb.denom;
    _lastEncodeDurationMs = (double)ns / 1000000.0;
}

#pragma mark Output callback

static void OCEncodeOutputCallback(void *outputCallbackRefCon,
                                   void *sourceFrameRefCon,
                                   OSStatus status,
                                   VTEncodeInfoFlags infoFlags,
                                   CMSampleBufferRef sampleBuffer) {
    if (status != noErr || !sampleBuffer) {
        NSLog(@"[OmniCam] encode callback error: %d", (int)status);
        return;
    }
    OCEncoder *enc = (__bridge OCEncoder *)outputCallbackRefCon;
    if (!enc || !enc->_running) return;
    CFRetain(sampleBuffer);
    dispatch_async(enc->_cbQueue, ^{
        [enc handleOutputSampleBuffer:sampleBuffer];
        CFRelease(sampleBuffer);
    });
}

- (void)handleOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer {
    if (!_running) return;
    id<OCEncoderDelegate> d = _delegate;
    if (!d) return;

    CMBlockBufferRef block = CMSampleBufferGetDataBuffer(sampleBuffer);
    if (!block) return;

    // --- Parameter sets (SPS/PPS) come from the format description's 'avcC' atom
    // (the Get(NumberOf)ParameterSets API does not exist in the iOS 12.4 SDK).
    CMFormatDescriptionRef desc = CMSampleBufferGetFormatDescription(sampleBuffer);
    if (desc) {
        NSData *sps = nil, *pps = nil;
        if (OCParseAvcCParameterSets(desc, &sps, &pps) &&
            (![_cachedSPS isEqualToData:sps] || ![_cachedPPS isEqualToData:pps])) {
            _cachedSPS = sps;
            _cachedPPS = pps;
            NSLog(@"[OmniCam] encoder parameter sets updated (sps %lu B, pps %lu B)",
                  (unsigned long)sps.length, (unsigned long)pps.length);
            if ([d respondsToSelector:@selector(encoder:didUpdateParameterSetsSPS:PPS:)]) {
                [d encoder:self didUpdateParameterSetsSPS:sps PPS:pps];
            }
        }
    }

    // --- AVCC (4-byte BE length prefixes) → Annex-B start codes.
    size_t total = CMBlockBufferGetDataLength(block);
    if (total < 5) return;
    uint8_t *raw = (uint8_t *)malloc(total);
    if (!raw) return;
    OSStatus st = CMBlockBufferCopyDataBytes(block, 0, total, raw);
    if (st != kCMBlockBufferNoErr) {
        free(raw);
        return;
    }

    NSMutableData *annexB = [NSMutableData dataWithCapacity:total + 16];
    BOOL isIDR = NO;
    NSUInteger off = 0;
    static const uint8_t sc[4] = {0x00, 0x00, 0x00, 0x01};
    while (off + 4 <= (NSUInteger)total) {
        uint32_t nalLen = ((uint32_t)raw[off] << 24) | ((uint32_t)raw[off + 1] << 16) |
                          ((uint32_t)raw[off + 2] << 8) | (uint32_t)raw[off + 3];
        off += 4;
        if (nalLen == 0 || off + nalLen > (NSUInteger)total) break; // tolerate padding
        const uint8_t *nal = raw + off;
        uint8_t nalType = nal[0] & 0x1F;
        if (nalType == 5) isIDR = YES;
        if (nalType >= 1 && nalType <= 5) { // slices only; skip SEI/AUD emitted by VT
            [annexB appendBytes:sc length:4];
            [annexB appendBytes:nal length:nalLen];
        }
        off += nalLen;
    }
    free(raw);

    if (annexB.length == 0) return;

    CMTime pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer);
    CMTime dts = CMSampleBufferGetDecodeTimeStamp(sampleBuffer);
    double ptsMs = CMTimeGetSeconds(pts) * 1000.0;
    double dtsMs = CMTIME_IS_NUMERIC(dts) ? CMTimeGetSeconds(dts) * 1000.0 : ptsMs;

    [d encoder:self didProduceAnnexB:annexB isIDR:isIDR ptsMs:ptsMs dtsMs:dtsMs];
}

@end
