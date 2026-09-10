//
//  OCFilterPipeline.h — Core Image filter chain (EAGL buffer renders, Metal preview).
//
//  Frame path: OCCaptureEngine delegate input (NV12 CVPixelBuffer) → filter chain
//  per DECISIONS.md order (geometry → adjust → look → LUT → stylize → beauty →
//  overlay) → render into the pipeline's OWN GLES-compatible pool → delegate.
//  Identity states take a zero-copy pass-through path.
//  iOS 12 BUG 1 fix: buffer renders use an EAGL-backed CIContext (Metal into
//  VT-pool buffers is not guaranteeable — AVCamFilter pattern); the Metal
//  device/context is preview-only.
#import <Foundation/Foundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>
#import <CoreImage/CoreImage.h>
#import "OCCaptureEngine.h"

NS_ASSUME_NONNULL_BEGIN

@class OCFilterPipeline, OCFilterState, OCEncoder;

@protocol OCFilterPipelineDelegate <NSObject>
/// Called on the pipeline's serial render queue. Buffer valid for the call only.
- (void)filterPipeline:(OCFilterPipeline *)pipeline
 didOutputPixelBuffer:(CVPixelBufferRef _Nonnull)pixelBuffer
             timestamp:(CMTime)timestamp;
@end

@protocol OCFilterPipelinePreviewDelegate <NSObject>
@optional
/// Called after every processed frame (identity or filtered); receiver decides whether to redraw.
- (void)filterPipelineDidRenderFrame:(OCFilterPipeline *)pipeline;
@end

@interface OCFilterPipeline : NSObject <OCCaptureEngineDelegate>

@property (nonatomic, weak, nullable) id<OCFilterPipelineDelegate> delegate;
@property (nonatomic, weak, nullable) id<OCFilterPipelinePreviewDelegate> previewDelegate;
/// Historical: used to feed the encoder's CVPixelBufferPool to the pipeline.
/// Since the iOS 12 EAGL fix, filtered frames render into the pipeline's own
/// GLES-compatible pool instead; kept so the engine graph wiring stays intact.
@property (nonatomic, weak, nullable) OCEncoder *encoder;

@property (nonatomic, strong, readonly, nullable) id<MTLDevice> metalDevice;
@property (nonatomic, strong, readonly) CIContext *ciContext;
/// Command queue on metalDevice for the WYSIWYG MTKView preview.
@property (nonatomic, strong, readonly, nullable) id<MTLCommandQueue> commandQueue;
@property (nonatomic, readonly) double lastRenderMs;

- (instancetype)initWithFilterState:(OCFilterState *)state NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

/// Synchronous one-shot render for callers off the hot path (WYSIWYG snapshot).
- (void)renderFrame:(CVPixelBufferRef _Nonnull)input
         intoBuffer:(CVPixelBufferRef _Nonnull)output
              state:(OCFilterState *)state;

/// Latest composed output CIImage (retains its source buffers; draw promptly).
- (nullable CIImage *)lastPreviewImage;

- (void)clearLutCache;

@end

NS_ASSUME_NONNULL_END
