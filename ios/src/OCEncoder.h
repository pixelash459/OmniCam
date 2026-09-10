//
//  OCEncoder.h — VTCompressionSession H.264 wrapper (PROTOCOL.md §7).
//
#import <Foundation/Foundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <VideoToolbox/VideoToolbox.h>

#import "OCFilterPipeline.h"

NS_ASSUME_NONNULL_BEGIN

@class OCEncoder;

@protocol OCEncoderDelegate <NSObject>
/// Called on the encoder's serial callback queue, in completion order.
- (void)encoder:(OCEncoder *)encoder
 didProduceAnnexB:(NSData *)annexB
           isIDR:(BOOL)isIDR
           ptsMs:(double)ptsMs
           dtsMs:(double)dtsMs;
@optional
/// SPS/PPS re-extracted on format change; delivered before the first frame that uses them.
- (void)encoder:(OCEncoder *)encoder didUpdateParameterSetsSPS:(NSData *)sps PPS:(NSData *)pps;
@end

@interface OCEncoder : NSObject <OCFilterPipelineDelegate>

@property (nonatomic, weak, nullable) id<OCEncoderDelegate> delegate;
@property (nonatomic, readonly, getter=isRunning) BOOL running;
/// Encode time of the most recent frame (for the stats message `enc_ms`).
@property (nonatomic, readonly) double lastEncodeDurationMs;

/// Synchronous. Hardware-accelerated H.264 Main, RealTime, no frame reordering.
- (BOOL)startWithWidth:(int)width
                height:(int)height
                   fps:(int)fps
           bitrateKbps:(int)kbps
                keyint:(int)keyint
                 error:(NSError **)error;

- (void)stop;

/// May be called from any thread (serialized internally). If the buffer dimensions
/// differ from the session, the session is recreated automatically.
- (void)encodePixelBuffer:(CVPixelBufferRef _Nonnull)buffer timestamp:(CMTime)pts;

/// Live bitrate change (AverageBitRate + DataRateLimits). Synchronous.
- (void)setBitrateKbps:(int)kbps;

/// Forces kVTEncodeFrameOptionKey_ForceKeyFrame on the next encoded frame.
- (void)forceKeyframe;

/// Returns a +1 retained pool ref or NULL. Do NOT cache: it becomes invalid when
/// the session is (re)created. Dispatches internally to keep access race-free.
- (nullable CVPixelBufferPoolRef)currentPixelBufferPool CF_RETURNS_RETAINED;

@end

NS_ASSUME_NONNULL_END
