//
//  OCFilterPipeline.m — full CIFilter chain, EAGL-rendered encoder output, overlay cache.
//
#import "OCFilterPipeline.h"
#import "OCFilterState.h"
#import "OCLutLoader.h"
#import "OCEncoder.h"

#import <UIKit/UIKit.h> // UIGraphicsImageRenderer for the overlay bitmap
#import <OpenGLES/EAGL.h> // BUG 1 FIX: EAGL-backed CIContext for buffer renders
#import <mach/mach_time.h>

static uint64_t ocNowMs(void) {
    static mach_timebase_info_data_t tb;
    if (tb.denom == 0) mach_timebase_info(&tb);
    return mach_absolute_time() * tb.numer / tb.denom / 1000000ull;
}

@interface OCFilterPipeline ()
@property (nonatomic, strong) OCFilterState *state;
// Metal backend: MTKView WYSIWYG preview ONLY (exposed to the view controller).
@property (nonatomic, strong, nullable) id<MTLDevice> metalDevice;
@property (nonatomic, strong, nullable) id<MTLCommandQueue> commandQueue;
@property (nonatomic, strong) CIContext *ciContext;
// BUG 1 FIX (iOS 12 device crash): EAGL-backed CIContext for CVPixelBuffer
// renders. Rendering Metal into NV12 buffers from the VTCompressionSession pool
// is not guaranteeable on iOS 12; Apple's AVCamFilter sample uses an OpenGL ES
// context for exactly this pipeline.
@property (nonatomic, strong, nullable) EAGLContext *eaglContext;
@property (nonatomic, strong, nullable) CIContext *eaglCIContext;
@property (nonatomic, assign) CGColorSpaceRef colorSpace;
// Diagnostics throttle: logs the first successful filtered render only.
@property (nonatomic, assign) BOOL loggedFirstFilteredRender;
@property (nonatomic, assign) BOOL loggedProcessException;
@property (nonatomic, strong) dispatch_queue_t renderQueue;
@property (nonatomic, strong) OCLutLoader *lutLoader;
@property (nonatomic, assign) double lastRenderMs;

// Preview handoff (written on renderQueue, read from MTKView's queue).
@property (nonatomic, strong, nullable) CIImage *previewImage;
@property (nonatomic, strong) NSLock *previewLock;

// Destination pool for FILTERED frames, recreated when dimensions change.
// BUG 1 FIX: its buffers carry kCVPixelBufferOpenGLESCompatibilityKey so the
// EAGL-backed CIContext can render into them. The encoder's VTCompressionSession
// pool is never used for filtered output anymore (Metal compat on VT-pool
// buffers is unguaranteeable on iOS 12); identity frames never touch this pool
// at all — capture buffers go straight to the encoder, zero-copy.
@property (nonatomic, assign, nullable) CVPixelBufferPoolRef ownPool;
@property (nonatomic, assign) size_t ownPoolWidth;
@property (nonatomic, assign) size_t ownPoolHeight;

// Overlay cache: rebuilt when text / timecode second / frame width change.
@property (nonatomic, strong, nullable) CIImage *overlayImage;
@property (nonatomic, copy, nullable) NSString *overlayCacheKey;
@property (nonatomic, assign) CGFloat overlayCacheWidth;

// Lazy UTC timecode formatter (HH:mm:ss).
@property (nonatomic, strong, nullable) NSDateFormatter *tcFormatter;
@end

@implementation OCFilterPipeline

- (instancetype)initWithFilterState:(OCFilterState *)state {
    NSAssert(state != nil, @"OCFilterPipeline requires a filter state");
    self = [super init];
    if (!self) return nil;

    _state = state;

    // BUG 1 FIX (device crash): a Metal-backed CIContext rendering into NV12
    // CVPixelBuffers from the VTCompressionSession pool is not guaranteed to
    // work on iOS 12 — Apple's AVCamFilter sample uses an EAGL (OpenGL ES)
    // context for exactly this pipeline. Filtered encoder-path renders therefore
    // go through an EAGL-backed CIContext; the Metal context below is kept ONLY
    // for the MTKView preview.
    _eaglContext = [[EAGLContext alloc] initWithAPI:kEAGLRenderingAPIOpenGLES3];
    if (!_eaglContext) {
        // GLES3 context creation can fail on downlevel parts; GLES2 is the
        // AVCamFilter-era floor and covers everything this chain renders.
        _eaglContext = [[EAGLContext alloc] initWithAPI:kEAGLRenderingAPIOpenGLES2];
    }
    if (_eaglContext) {
        _eaglCIContext = [CIContext contextWithEAGLContext:_eaglContext];
    }

    // Metal stays preview-only: it never renders into VT-pool buffers.
    _metalDevice = MTLCreateSystemDefaultDevice();
    if (_metalDevice) {
        _ciContext = [CIContext contextWithMTLDevice:_metalDevice];
        _commandQueue = [_metalDevice newCommandQueue];
    }
    if (!_eaglCIContext && !_ciContext) {
        // Neither backend initialized (should not happen on A8, but stay
        // functional with a CPU CIContext).
        _ciContext = [CIContext contextWithOptions:nil];
    }

    // Device RGB is correct for NV12 destinations: CI reads the buffer's format
    // tag and performs the YCbCr conversion itself (no explicit BT.601/709 work).
    _colorSpace = CGColorSpaceCreateDeviceRGB();

    // Diagnostics: confirm on-device which render backend is active (BUG 1 fix).
    NSLog(@"[OCFilterPipeline] init: buffer renders via %@, preview via %@",
          _eaglCIContext
              ? [NSString stringWithFormat:@"EAGL CIContext (GLES%ld)",
                    (long)(_eaglContext.API == kEAGLRenderingAPIOpenGLES3 ? 3 : 2)]
              : @"software fallback CIContext",
          _metalDevice ? @"Metal CIContext (MTKView only)" : @"none");

    _renderQueue = dispatch_queue_create("oc.filter.render", DISPATCH_QUEUE_SERIAL);
    _lutLoader = [[OCLutLoader alloc] init];
    _previewLock = [[NSLock alloc] init];
    _lastRenderMs = 0;
    return self;
}

- (void)dealloc {
    if (_colorSpace) CGColorSpaceRelease(_colorSpace);
    if (_ownPool) CFRelease(_ownPool);
}

- (CIContext *)ciContext { return _ciContext; }

// CIContext used for CVPixelBuffer renders: EAGL-backed when available (the
// iOS 12 fix), Metal/software only as a last-resort fallback.
- (CIContext *)bufferRenderContext {
    return _eaglCIContext ?: _ciContext;
}

- (void)clearLutCache {
    dispatch_async(_renderQueue, ^{
        [self->_lutLoader clearCache];
    });
}

#pragma mark OCCaptureEngineDelegate (hot path)

- (void)captureEngine:(OCCaptureEngine *)engine
  didOutputPixelBuffer:(CVPixelBufferRef)pixelBuffer
             timestamp:(CMTime)timestamp {
    CFRetain(pixelBuffer); // outlives this call: we hop to the render queue
    dispatch_async(_renderQueue, ^{
        [self processBuffer:pixelBuffer timestamp:timestamp];
        CFRelease(pixelBuffer);
    });
}

- (void)processBuffer:(CVPixelBufferRef)input timestamp:(CMTime)ts {
    OCFilterState *st = _state;
    id<OCFilterPipelineDelegate> d = _delegate;

    // CRITICAL PERF RULE: identity state → hand the capture buffer straight to the
    // encoder. Zero filters, zero copies; the buffer stays in the capture pool.
    if (st.isIdentity) {
        CIImage *img = [CIImage imageWithCVImageBuffer:input];
        [self setPreviewImage:img];
        if (d) [d filterPipeline:self didOutputPixelBuffer:input timestamp:ts];
        dispatch_async(dispatch_get_main_queue(), ^{
            [self notifyPreviewDelegate];
        });
        return;
    }

    uint64_t t0 = ocNowMs();
    CIImage *inImage = [CIImage imageWithCVImageBuffer:input];
    CGRect wext = CGRectZero;
    CIImage *result = nil;
    CVPixelBufferRef dest = NULL;
    @try {
        result = [self processImage:inImage state:st outExtent:&wext];

        dest = [self acquireDestinationBufferForInput:input];
        if (!dest) {
            // Pool exhaustion: ship the unfiltered frame rather than dropping it.
            if (d) [d filterPipeline:self didOutputPixelBuffer:input timestamp:ts];
            [self setPreviewImage:inImage];
            dispatch_async(dispatch_get_main_queue(), ^{
                [self notifyPreviewDelegate];
            });
            return;
        }

        // Render bounds must never exceed the destination buffer — the rotate-90
        // geometry swap can make the working extent outgrow dest. Intersect; if the
        // intersection is empty, fall back to the unfiltered pass-through rather
        // than letting CI write out of bounds.
        CGRect dstBounds = CGRectMake(0, 0,
                                      (CGFloat)CVPixelBufferGetWidth(dest),
                                      (CGFloat)CVPixelBufferGetHeight(dest));
        CGRect renderBounds = CGRectIntersection(wext, dstBounds);
        if (CGRectIsEmpty(renderBounds)) {
            CFRelease(dest); // acquired CF_RETURNS_RETAINED - do not leak it
            dest = NULL;
            if (d) [d filterPipeline:self didOutputPixelBuffer:input timestamp:ts];
            [self setPreviewImage:inImage];
            dispatch_async(dispatch_get_main_queue(), ^{
                [self notifyPreviewDelegate];
            });
            return;
        }

        [self clearNV12:dest];
        // EAGLContext is not thread-safe; CI render without current context crashes/aborts on iOS 12.
        if (_eaglContext) [EAGLContext setCurrentContext:_eaglContext];
        [[self bufferRenderContext] render:result toCVPixelBuffer:dest bounds:renderBounds colorSpace:_colorSpace];
        if (!_loggedFirstFilteredRender) {
            // Throttled diagnostics: fires once, proving the GLES render path works
            // and naming the destination buffer dimensions.
            _loggedFirstFilteredRender = YES;
            NSLog(@"[OCFilterPipeline] first filtered frame rendered OK: dest %zux%zu px, context %@",
                  CVPixelBufferGetWidth(dest), CVPixelBufferGetHeight(dest),
                  _eaglCIContext ? @"EAGL" : @"software fallback");
        }
        if (d) [d filterPipeline:self didOutputPixelBuffer:dest timestamp:ts];
        CFRelease(dest);
        dest = NULL;

        _lastRenderMs = (double)(ocNowMs() - t0);
        [self setPreviewImage:result];
        dispatch_async(dispatch_get_main_queue(), ^{
            [self notifyPreviewDelegate];
        });
    } @catch (NSException *ex) {
        if (dest) {
            CFRelease(dest);
            dest = NULL;
        }
        if (!_loggedProcessException) {
            _loggedProcessException = YES;
            NSLog(@"[OCFilterPipeline] processBuffer exception: %@", ex);
        }
        if (d) [d filterPipeline:self didOutputPixelBuffer:input timestamp:ts];
        [self setPreviewImage:inImage];
        dispatch_async(dispatch_get_main_queue(), ^{
            [self notifyPreviewDelegate];
        });
    }
}

- (void)notifyPreviewDelegate {
    id<OCFilterPipelinePreviewDelegate> p = _previewDelegate;
    if (p && [p respondsToSelector:@selector(filterPipelineDidRenderFrame:)]) {
        [p filterPipelineDidRenderFrame:self];
    }
}

- (void)setPreviewImage:(CIImage *)image {
    [_previewLock lock];
    _previewImage = image;
    [_previewLock unlock];
}

- (nullable CIImage *)lastPreviewImage {
    [_previewLock lock];
    CIImage *img = _previewImage;
    [_previewLock unlock];
    return img;
}

#pragma mark Destination buffers

- (nullable CVPixelBufferRef)acquireDestinationBufferForInput:(CVPixelBufferRef)input CF_RETURNS_RETAINED {
    size_t w = CVPixelBufferGetWidth(input);
    size_t h = CVPixelBufferGetHeight(input);

    // BUG 1 FIX: filtered frames render into the OWN pool, whose buffers are
    // created with kCVPixelBufferOpenGLESCompatibilityKey so the EAGL-backed
    // CIContext can draw into them. The encoder's VTCompressionSession pool is
    // never used for filtered frames — Metal compat on VT-pool buffers is
    // unguaranteeable on iOS 12 (AVCamFilter pattern). This method is only
    // reached when filtering is active; identity frames hand the capture buffer
    // straight to the encoder (zero-copy, no pool here).
    if (!_ownPool || _ownPoolWidth != w || _ownPoolHeight != h) {
        if (_ownPool) {
            CFRelease(_ownPool);
            _ownPool = NULL;
        }
        NSDictionary *attrs = @{
            (id)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
            (id)kCVPixelBufferWidthKey : @(w),
            (id)kCVPixelBufferHeightKey : @(h),
            (id)kCVPixelBufferOpenGLESCompatibilityKey : @YES,
            (id)kCVPixelBufferMetalCompatibilityKey : @YES,
        };
        CVPixelBufferPoolRef created = NULL;
        if (CVPixelBufferPoolCreate(kCFAllocatorDefault, NULL, (__bridge CFDictionaryRef)attrs, &created) == kCVReturnSuccess) {
            _ownPool = created; // pool retains; we own one ref
            _ownPoolWidth = w;
            _ownPoolHeight = h;
        }
    }
    if (_ownPool) {
        CVPixelBufferRef out = NULL;
        if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, _ownPool, &out) == kCVReturnSuccess) {
            return out;
        }
    }
    // Pool exhausted/unavailable: the caller ships the frame unfiltered instead
    // of dropping it.
    return NULL;
}

// Destination-pool buffers are recycled; stale content would ghost into frames
// whose filter chain leaves gaps (e.g. zoom-out / rotation), so pre-fill legal
// NV12 black.
- (void)clearNV12:(CVPixelBufferRef)buf {
    if (CVPixelBufferLockBaseAddress(buf, 0) != kCVReturnSuccess) return;
    uint8_t *y = CVPixelBufferGetBaseAddressOfPlane(buf, 0);
    size_t ys = CVPixelBufferGetBytesPerRowOfPlane(buf, 0);
    size_t yh = CVPixelBufferGetHeightOfPlane(buf, 0);
    if (y) memset(y, 16, ys * yh); // video-range black
    uint8_t *c = CVPixelBufferGetBaseAddressOfPlane(buf, 1);
    size_t cs = CVPixelBufferGetBytesPerRowOfPlane(buf, 1);
    size_t ch = CVPixelBufferGetHeightOfPlane(buf, 1);
    if (c) memset(c, 128, cs * ch); // neutral chroma
    CVPixelBufferUnlockBaseAddress(buf, 0);
}

#pragma mark Filter chain (DECISIONS.md order)

- (CIImage *)applyFilter:(NSString *)name input:(CIImage *)in setup:(void (^)(CIFilter *))setup {
    CIFilter *f = [CIFilter filterWithName:name];
    if (!f) return in;
    @try {
        [f setValue:in forKey:kCIInputImageKey];
        if (setup) setup(f);
        return f.outputImage ?: in;
    } @catch (NSException *ex) {
        NSLog(@"[OCFilterPipeline] filter %@ exception: %@", name, ex);
        return in;
    }
}

- (CIImage *)crop:(CIImage *)img toRect:(CGRect)rect {
    return [self applyFilter:@"CICrop"
                       input:img
                       setup:^(CIFilter *f) {
        [f setValue:[CIVector vectorWithCGRect:rect] forKey:@"inputRectangle"];
    }];
}

// Builds the full chain for one frame. Returns the composed image and the "working
// extent" (the region to render into the destination buffer).
- (CIImage *)processImage:(CIImage *)input state:(OCFilterState *)st outExtent:(CGRect *)outExtent {
    CIImage *img = input;
    CGRect orig = img.extent;
    CGFloat w = orig.size.width;
    CGFloat h = orig.size.height;
    CGPoint center = CGPointMake(CGRectGetMidX(orig), CGRectGetMidY(orig));

    // ---- 1. Geometry: mirror / flipV / rotate / zoom / pan, then aspect crop.
    BOOL geom = st.mirror || st.flipV || st.rotate != 0 ||
                fabs(st.zoom - 1.0) > 1e-4 || fabs(st.panX) > 1e-4 || fabs(st.panY) > 1e-4 ||
                ![st.aspect isEqualToString:@"native"];
    if (geom) {
        // Scale (with mirror/flip) first, then rotate — both around the center;
        // pan is a fraction of the original size applied afterwards.
        CGAffineTransform t = CGAffineTransformMakeTranslation(center.x, center.y);
        t = CGAffineTransformRotate(t, (CGFloat)st.rotate * (CGFloat)M_PI / 180.0f);
        t = CGAffineTransformScale(t, st.zoom * (st.mirror ? -1.0 : 1.0), st.zoom * (st.flipV ? -1.0 : 1.0));
        t = CGAffineTransformTranslate(t, -center.x, -center.y);
        t = CGAffineTransformTranslate(t, st.panX * w, st.panY * h);
        img = [self applyFilter:@"CIAffineTransform" input:img setup:^(CIFilter *f) {
            [f setValue:[NSValue valueWithCGAffineTransform:t] forKey:@"inputTransform"];
        }];

        CGFloat ar = [st.aspect isEqualToString:@"16:9"] ? 16.0 / 9.0
                   : [st.aspect isEqualToString:@"4:3"]  ? 4.0 / 3.0 : 0.0;
        if (ar > 0.0) {
            CGRect e = img.extent;
            CGFloat cur = e.size.width / e.size.height;
            CGRect crop = e;
            if (cur > ar) {
                crop.size.width = floor(e.size.height * ar);
                crop.origin.x = e.origin.x + floor((e.size.width - crop.size.width) / 2.0);
            } else {
                crop.size.height = floor(e.size.width / ar);
                crop.origin.y = e.origin.y + floor((e.size.height - crop.size.height) / 2.0);
            }
            img = [self crop:img toRect:crop];
        }
    }
    CGRect wext = img.extent;

    // ---- 2. Adjust.
    if (st.brightness != 0.0 || st.contrast != 0.0 || st.saturation != 1.0) {
        CGFloat contrast = MAX(0.05, 1.0 + st.contrast); // CIColorControls contrast is 0..2, 1 = neutral
        img = [self applyFilter:@"CIColorControls" input:img setup:^(CIFilter *f) {
            [f setValue:@(st.saturation) forKey:@"inputSaturation"];
            [f setValue:@(st.brightness) forKey:@"inputBrightness"];
            [f setValue:@(contrast) forKey:@"inputContrast"];
        }];
    }
    if (st.temperature != 0.0) {
        // CITemperatureAndTint: inputNeutral / inputTargetNeutral (CIVector x=temp y=tint).
        CGFloat targetNeutral = 6500.0 + st.temperature * 3000.0;
        img = [self applyFilter:@"CITemperatureAndTint" input:img setup:^(CIFilter *f) {
            [f setValue:[CIVector vectorWithX:6500 Y:0] forKey:@"inputNeutral"];
            [f setValue:[CIVector vectorWithX:targetNeutral Y:0] forKey:@"inputTargetNeutral"];
        }];
    }
    if (st.vibrance != 0.0) {
        img = [self applyFilter:@"CIVibrance" input:img setup:^(CIFilter *f) {
            [f setValue:@(st.vibrance) forKey:@"inputAmount"];
        }];
    }
    if (fabs(st.gamma - 1.0) > 1e-4) {
        img = [self applyFilter:@"CIGammaAdjust" input:img setup:^(CIFilter *f) {
            [f setValue:@(st.gamma) forKey:@"inputPower"];
        }];
    }
    if (st.sharpness != 0.0) {
        img = [self applyFilter:@"CIUnsharpMask" input:img setup:^(CIFilter *f) {
            [f setValue:@2.0 forKey:@"inputRadius"];
            [f setValue:@(st.sharpness * 2.0) forKey:@"inputIntensity"];
        }];
    }
    if (st.vignette != 0.0) {
        img = [self applyFilter:@"CIVignette" input:img setup:^(CIFilter *f) {
            [f setValue:@(st.vignette * 2.0) forKey:@"inputIntensity"];
            [f setValue:@1.5 forKey:@"inputRadius"];
        }];
    }

    // ---- 3. Look.
    NSString *look = st.look;
    if ([look isEqualToString:@"mono"]) {
        img = [self applyFilter:@"CIPhotoEffectMono" input:img setup:NULL];
    } else if ([look isEqualToString:@"noir"]) {
        img = [self applyFilter:@"CIPhotoEffectNoir" input:img setup:NULL];
    } else if ([look isEqualToString:@"chrome"]) {
        img = [self applyFilter:@"CIPhotoEffectChrome" input:img setup:NULL];
    } else if ([look isEqualToString:@"fade"]) {
        img = [self applyFilter:@"CIPhotoEffectFade" input:img setup:NULL];
    } else if ([look isEqualToString:@"instant"]) {
        img = [self applyFilter:@"CIPhotoEffectInstant" input:img setup:NULL];
    } else if ([look isEqualToString:@"process"]) {
        img = [self applyFilter:@"CIPhotoEffectProcess" input:img setup:NULL];
    } else if ([look isEqualToString:@"transfer"]) {
        img = [self applyFilter:@"CIPhotoEffectTransfer" input:img setup:NULL];
    } else if ([look isEqualToString:@"sepia"]) {
        img = [self applyFilter:@"CISepiaTone" input:img setup:^(CIFilter *f) {
            [f setValue:@0.9 forKey:@"inputIntensity"];
        }];
    } else if ([look isEqualToString:@"invert"]) {
        img = [self applyFilter:@"CIColorInvert" input:img setup:NULL];
    } else if ([look isEqualToString:@"false_color"]) {
        img = [self applyFilter:@"CIFalseColor" input:img setup:^(CIFilter *f) {
            [f setValue:[CIColor colorWithRed:0 green:0 blue:0 alpha:1] forKey:@"inputColor0"];
            [f setValue:[CIColor colorWithRed:0.7 green:1.0 blue:0.3 alpha:1] forKey:@"inputColor1"];
        }];
    }

    // ---- 4. LUT (.cube via CIColorCube).
    if (st.lut.length > 0) {
        CIImage *luted = [_lutLoader applyLutNamed:st.lut toImage:img];
        if (luted) img = luted;
    }

    // ---- 5. Stylize.
    NSString *sz = st.stylize;
    CGFloat amt = st.stylizeAmount;
    CGPoint wcenter = CGPointMake(CGRectGetMidX(wext), CGRectGetMidY(wext));
    CGFloat wmax = MAX(wext.size.width, wext.size.height);
    if ([sz isEqualToString:@"pixelate"]) {
        img = [self applyFilter:@"CIPixellate" input:img setup:^(CIFilter *f) {
            [f setValue:@(4.0 + amt * 36.0) forKey:@"inputScale"];
            [f setValue:[CIVector vectorWithX:wcenter.x Y:wcenter.y] forKey:@"inputCenter"];
        }];
    } else if ([sz isEqualToString:@"crystallize"]) {
        img = [self applyFilter:@"CICrystallize" input:img setup:^(CIFilter *f) {
            [f setValue:@(4.0 + amt * 36.0) forKey:@"inputRadius"];
            [f setValue:[CIVector vectorWithX:wcenter.x Y:wcenter.y] forKey:@"inputCenter"];
        }];
    } else if ([sz isEqualToString:@"hexagonal"]) {
        img = [self applyFilter:@"CIHexagonalPixellate" input:img setup:^(CIFilter *f) {
            [f setValue:@(4.0 + amt * 36.0) forKey:@"inputScale"];
            [f setValue:[CIVector vectorWithX:wcenter.x Y:wcenter.y] forKey:@"inputCenter"];
        }];
    } else if ([sz isEqualToString:@"twirl"]) {
        img = [self applyFilter:@"CITwirlDistortion" input:img setup:^(CIFilter *f) {
            [f setValue:[CIVector vectorWithX:wcenter.x Y:wcenter.y] forKey:@"inputCenter"];
            [f setValue:@(wmax * 0.75) forKey:@"inputRadius"];
            [f setValue:@(amt * 1.5 * (CGFloat)M_PI) forKey:@"inputAngle"];
        }];
    } else if ([sz isEqualToString:@"bulge"]) {
        img = [self applyFilter:@"CIBulgeDistortion" input:img setup:^(CIFilter *f) {
            [f setValue:[CIVector vectorWithX:wcenter.x Y:wcenter.y] forKey:@"inputCenter"];
            [f setValue:@(wmax * 0.75) forKey:@"inputRadius"];
            [f setValue:@(0.2 + amt * 0.8) forKey:@"inputScale"];
        }];
    } else if ([sz isEqualToString:@"bump"]) {
        img = [self applyFilter:@"CIBumpDistortion" input:img setup:^(CIFilter *f) {
            [f setValue:[CIVector vectorWithX:wcenter.x Y:(wcenter.y + wext.size.height * 0.15)] forKey:@"inputCenter"];
            [f setValue:@(wmax * 0.6) forKey:@"inputRadius"];
            [f setValue:@(0.2 + amt * 0.8) forKey:@"inputScale"];
        }];
    } else if ([sz isEqualToString:@"soft_blur"]) {
        img = [self applyFilter:@"CIGaussianBlur" input:img setup:^(CIFilter *f) {
            [f setValue:@(MIN(10.0, amt * 10.0)) forKey:@"inputRadius"]; // hard cap per DECISIONS.md
        }];
    } else if ([sz isEqualToString:@"zoom_blur"]) {
        img = [self applyFilter:@"CIZoomBlur" input:img setup:^(CIFilter *f) {
            [f setValue:@(amt * 8.0) forKey:@"inputAmount"];
            [f setValue:[CIVector vectorWithX:wcenter.x Y:wcenter.y] forKey:@"inputCenter"];
        }];
    }
    if (![sz isEqualToString:@"none"]) {
        img = [self crop:img toRect:wext]; // blur/distortion filters inflate the extent
    }

    // ---- 6. Beauty (YUCIHighPassSkinSmoothing-style, built-in filters only).
    if (st.beauty > 0.001) {
        CIImage *orig = img;
        CIImage *blurred = [self applyFilter:@"CIGaussianBlur" input:orig setup:^(CIFilter *f) {
            [f setValue:@8.0 forKey:@"inputRadius"];
        }];
        // highPass = orig − blurred + 0.5: bias the blur down half-way, then add.
        CIImage *biased = [self applyFilter:@"CIColorMatrix" input:blurred setup:^(CIFilter *f) {
            [f setValue:[CIVector vectorWithX:1 Y:0 Z:0 W:0] forKey:@"inputRVector"];
            [f setValue:[CIVector vectorWithX:0 Y:1 Z:0 W:0] forKey:@"inputGVector"];
            [f setValue:[CIVector vectorWithX:0 Y:0 Z:1 W:0] forKey:@"inputBVector"];
            [f setValue:[CIVector vectorWithX:0 Y:0 Z:0 W:1] forKey:@"inputAVector"];
            [f setValue:[CIVector vectorWithX:-0.5 Y:-0.5 Z:-0.5 W:0] forKey:@"inputBiasVector"];
        }];
        CIImage *highPass = [self applyFilter:@"CIAdditionCompositing" input:orig setup:^(CIFilter *f) {
            [f setValue:biased forKey:kCIInputBackgroundImageKey];
        }];
        CIImage *smoothed = [self applyFilter:@"CIHardLightBlendMode" input:orig setup:^(CIFilter *f) {
            [f setValue:highPass forKey:kCIInputBackgroundImageKey];
        }];
        // Restrict smoothing to mid-tones (skin): band = min(lum, 1−lum), boosted.
        CIImage *lum = [self applyFilter:@"CIColorControls" input:orig setup:^(CIFilter *f) {
            [f setValue:@0.0 forKey:@"inputSaturation"];
        }];
        CIImage *inv = [self applyFilter:@"CIColorInvert" input:lum setup:NULL];
        CIImage *band = [self applyFilter:@"CIMinimumCompositing" input:lum setup:^(CIFilter *f) {
            [f setValue:inv forKey:kCIInputBackgroundImageKey];
        }];
        band = [self applyFilter:@"CIColorMatrix" input:band setup:^(CIFilter *f) {
            [f setValue:[CIVector vectorWithX:4 Y:0 Z:0 W:0] forKey:@"inputRVector"];
            [f setValue:[CIVector vectorWithX:0 Y:4 Z:0 W:0] forKey:@"inputGVector"];
            [f setValue:[CIVector vectorWithX:0 Y:0 Z:4 W:0] forKey:@"inputBVector"];
            [f setValue:[CIVector vectorWithX:0 Y:0 Z:0 W:1] forKey:@"inputAVector"];
        }];
        CIImage *masked = [self applyFilter:@"CIBlendWithMask" input:smoothed setup:^(CIFilter *f) {
            [f setValue:orig forKey:kCIInputBackgroundImageKey];
            [f setValue:band forKey:@"inputMaskImage"];
        }];
        // Intensity: alpha-blend the smoothed result over the original by `beauty`.
        CGFloat b = st.beauty;
        CIImage *faded = [self applyFilter:@"CIColorMatrix" input:masked setup:^(CIFilter *f) {
            [f setValue:[CIVector vectorWithX:b Y:0 Z:0 W:0] forKey:@"inputRVector"];
            [f setValue:[CIVector vectorWithX:0 Y:b Z:0 W:0] forKey:@"inputGVector"];
            [f setValue:[CIVector vectorWithX:0 Y:0 Z:b W:0] forKey:@"inputBVector"];
            [f setValue:[CIVector vectorWithX:0 Y:0 Z:0 W:b] forKey:@"inputAVector"];
        }];
        img = [self applyFilter:@"CISourceOverCompositing" input:faded setup:^(CIFilter *f) {
            [f setValue:orig forKey:kCIInputBackgroundImageKey];
        }];
    }

    // ---- 7. Overlay (cached bitmap; timecode text regenerates at 1 Hz).
    if (st.overlayText.length > 0 || st.showTimecode) {
        CIImage *ov = [self overlayImageForState:st width:wext.size.width];
        if (ov) {
            CGRect oext = ov.extent;
            CGRect place = CGRectMake(wext.origin.x,
                                      wext.origin.y + wext.size.height - oext.size.height,
                                      oext.size.width, oext.size.height);
            CIImage *placed = [ov imageByApplyingTransform:CGAffineTransformMakeTranslation(place.origin.x, place.origin.y)];
            img = [self applyFilter:@"CISourceOverCompositing" input:placed setup:^(CIFilter *f) {
                [f setValue:img forKey:kCIInputBackgroundImageKey];
            }];
        }
    }

    if (outExtent) *outExtent = wext;
    return img;
}

#pragma mark Overlay

- (NSString *)utcTimecode {
    static NSDateFormatter *fmt;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        fmt = [[NSDateFormatter alloc] init];
        fmt.dateFormat = @"HH:mm:ss";
        fmt.timeZone = [NSTimeZone timeZoneWithAbbreviation:@"UTC"]; // UTC timecode per spec
    });
    return [fmt stringFromDate:[NSDate date]];
}

- (nullable CIImage *)overlayImageForState:(OCFilterState *)st width:(CGFloat)width {
    NSString *tc = st.showTimecode ? [self utcTimecode] : nil;
    NSString *key = [NSString stringWithFormat:@"%@\001%@", st.overlayText ?: @"", tc ?: @""];
    if (_overlayCacheKey && _overlayImage &&
        [_overlayCacheKey isEqualToString:key] && fabs(_overlayCacheWidth - width) < 1.0) {
        return _overlayImage;
    }

    CGFloat scaleF = MAX(0.5, width / 1280.0);
    CGFloat font = MAX(16.0, 30.0 * scaleF);
    CGFloat pad = 10.0 * scaleF;

    NSMutableAttributedString *as = [[NSMutableAttributedString alloc] init];
    if (st.overlayText.length > 0) {
        [as appendAttributedString:[[NSAttributedString alloc]
            initWithString:st.overlayText
                attributes:@{NSFontAttributeName : [UIFont systemFontOfSize:font weight:UIFontWeightSemibold],
                             NSForegroundColorAttributeName : [UIColor whiteColor]}]];
    }
    if (tc.length > 0) {
        if (as.length > 0) {
            [as appendAttributedString:[[NSAttributedString alloc]
                initWithString:@"  \u2022  "
                    attributes:@{NSFontAttributeName : [UIFont systemFontOfSize:font weight:UIFontWeightRegular],
                                 NSForegroundColorAttributeName : [UIColor colorWithWhite:1.0 alpha:0.6]}]];
        }
        [as appendAttributedString:[[NSAttributedString alloc]
            initWithString:[tc stringByAppendingString:@" UTC"]
                attributes:@{NSFontAttributeName : [UIFont monospacedDigitSystemFontOfSize:font weight:UIFontWeightMedium],
                             NSForegroundColorAttributeName : [UIColor whiteColor]}]];
    }
    CGRect textBounds = [as boundingRectWithSize:CGSizeMake(CGFLOAT_MAX, CGFLOAT_MAX)
                                         options:NSStringDrawingUsesLineFragmentOrigin
                                         context:nil];
    CGSize size = CGSizeMake(ceil(textBounds.size.width) + pad * 2, ceil(textBounds.size.height) + pad);

    UIGraphicsImageRendererFormat *rfmt = [[UIGraphicsImageRendererFormat alloc] init];
    rfmt.scale = 1.0; // match frame pixels, not screen scale
    rfmt.opaque = NO;
    UIGraphicsImageRenderer *renderer = [[UIGraphicsImageRenderer alloc] initWithSize:size format:rfmt];
    UIImage *ui = [renderer imageWithActions:^(UIGraphicsImageRendererContext *rctx) {
        CGRect bg = CGRectMake(0, 0, size.width, size.height);
        UIBezierPath *path = [UIBezierPath bezierPathWithRoundedRect:bg cornerRadius:size.height * 0.25];
        [[UIColor colorWithWhite:0.0 alpha:0.45] setFill];
        [path fill];
        [as drawInRect:CGRectMake(pad, pad * 0.5, size.width - pad * 2, size.height - pad)];
    }];

    _overlayImage = [CIImage imageWithCGImage:ui.CGImage];
    _overlayCacheKey = key;
    _overlayCacheWidth = width;
    return _overlayImage;
}

#pragma mark One-shot render (WYSIWYG snapshots)

- (void)renderFrame:(CVPixelBufferRef)input intoBuffer:(CVPixelBufferRef)output state:(OCFilterState *)state {
    dispatch_sync(_renderQueue, ^{
        CIImage *inImage = [CIImage imageWithCVImageBuffer:input];
        CGRect wext = CGRectZero;
        CIImage *result = [self processImage:inImage state:state outExtent:&wext];
        CGRect dst = CGRectMake(0, 0, (CGFloat)CVPixelBufferGetWidth(output), (CGFloat)CVPixelBufferGetHeight(output));
        // Bounds must never exceed the destination buffer (rotate-90 swaps w/h).
        // Empty intersection → unfiltered pass-through instead of an out-of-
        // bounds render.
        CGRect bounds = CGRectIntersection(wext, dst);
        CIImage *toRender = result;
        if (CGRectIsEmpty(bounds)) {
            bounds = CGRectIntersection(inImage.extent, dst);
            toRender = inImage;
            if (CGRectIsEmpty(bounds)) return;
        }
        [self clearNV12:output];
        // EAGLContext is not thread-safe; CI render without current context crashes/aborts on iOS 12.
        if (self->_eaglContext) [EAGLContext setCurrentContext:self->_eaglContext];
        // EAGL-backed context (BUG 1 fix); Metal never renders into NV12 buffers.
        [[self bufferRenderContext] render:toRender toCVPixelBuffer:output bounds:bounds colorSpace:self->_colorSpace];
    });
}

- (double)lastRenderMs { return _lastRenderMs; }

@end
