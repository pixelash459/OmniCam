//
//  OCFilterPipeline.h — Metal-backed Core Image filter chain.
//
//  Frame path: OCCaptureEngine delegate input (NV12 CVPixelBuffer) → filter chain
//  per DECISIONS.md order (geometry → adjust → look → LUT → stylize → beauty →
//  overlay) → render into a CVPixelBuffer from the ENCODER's pool (fallback: own
//  pool) → delegate. Identity states take a zero-copy pass-through path.
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
/// Provides the output CVPixelBufferPool (usually the encoder's). Weak.
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
