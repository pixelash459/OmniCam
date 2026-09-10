//
//  OCCaptureEngine.h — single AVCaptureSession management.
//
//  HARD CONSTRAINT (DECISIONS.md): A8 can power only ONE camera at a time, and
//  AVCaptureMultiCamSession needs iOS 13 + A12. Therefore this engine owns exactly
//  ONE AVCaptureSession for the app's lifetime and pre-creates BOTH device inputs;
//  switching is beginConfiguration → removeInput/addInput → commitConfiguration
//  (~150-300 ms gap is expected and normal). Never create a second session.
#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>

NS_ASSUME_NONNULL_BEGIN

@class OCCaptureEngine;

@protocol OCCaptureEngineDelegate <NSObject>
/// Called on the engine's serial video queue. The buffer is only valid for the
/// duration of the call; retain (CFRetain) if escaping.
- (void)captureEngine:(OCCaptureEngine *)engine
  didOutputPixelBuffer:(CVPixelBufferRef _Nonnull)pixelBuffer
             timestamp:(CMTime)timestamp;
@end

@interface OCCaptureEngine : NSObject

@property (nonatomic, weak, nullable) id<OCCaptureEngineDelegate> delegate;
/// Optional; called on an internal queue.
@property (nonatomic, copy, nullable) void (^errorHandler)(NSString *message);

/// Exposed so the view controller can attach an AVCaptureVideoPreviewLayer.
@property (nonatomic, strong, readonly) AVCaptureSession *session;

@property (nonatomic, readonly, getter=isRunning) BOOL running;
/// "front" or "back" (PROTOCOL.md camera ids).
@property (nonatomic, copy, readonly) NSString *activeCameraId;
@property (nonatomic, readonly) BOOL wantsHighResolution; // 1080p preference (back only)
@property (nonatomic, readonly) CGFloat maxZoomFactor;    // active camera, clamped to 16
@property (nonatomic, readonly) CGFloat zoomFactor;
@property (nonatomic, readonly) BOOL torchSupported;
@property (nonatomic, readonly, getter=isTorchOn) BOOL torchOn;

/// Starts the session with the preset implied by activeCameraId + wantsHighResolution.
/// Synchronous (blocks while startRunning); callers may wrap in dispatch_async.
- (BOOL)startAndReturnError:(NSError **)error;
- (void)stop;
/// Re-starts after app interruptions (didBecomeActive).
- (void)ensureRunning;

/// Requests 1920x1080 preset for the next (re)configuration. Only applied while the
/// back camera is active — front caps at 720p (DECISIONS.md).
- (void)setWantsHighResolution:(BOOL)hd;

/// Instant single-session switch. Completion fires on an internal queue.
- (void)switchToCameraId:(NSString *)cameraId
              completion:(void (^_Nullable)(NSString *activeId, NSError * _Nullable error))completion;

/// Digital zoom via videoZoomFactor, clamped to activeFormat.videoMaxZoomFactor (and 16).
- (void)setZoomFactor:(CGFloat)factor;

/// Back camera only (checks hasTorch). Returns the actually-applied state.
- (BOOL)applyTorchOn:(BOOL)on;

@end

NS_ASSUME_NONNULL_END
