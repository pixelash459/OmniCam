//
//  OCNetManager.h — all networking, BSD sockets on GCD queues (no third-party code).
//
//  Ports per PROTOCOL.md §0:
//    9920 UDP beacon broadcast (phone → LAN, every 1 s)
//    9921 UDP video RTP (send) + NACK/PLI feedback (recv, same socket)
//    9923 TCP control (phone listens, ONE client; newline-delimited JSON)
//
//  Also owns the §6 adaptive bitrate driven by `rr` messages and the 1 Hz `stats`.
#import <Foundation/Foundation.h>
#import "OCFilterState.h"

NS_ASSUME_NONNULL_BEGIN

extern const int OCBeaconPort;   // 9920
extern const int OCVideoPort;    // 9921
extern const int OCControlPort;  // 9923

extern NSString * const OCMagicString;       // "OMNICAM1"
extern NSString * const OCAppVersionString;  // "1.1.10"

@class OCNetManager, OCCaptureEngine, OCEncoder, OCPacker;

@protocol OCNetManagerDelegate <NSObject>
@optional
- (void)netManagerClientDidConnect:(OCNetManager *)manager name:(NSString *)name;
- (void)netManagerClientDidDisconnect:(OCNetManager *)manager;
- (void)netManagerStreamingStateDidChange:(OCNetManager *)manager;
- (void)netManager:(OCNetManager *)manager activeCameraDidChange:(NSString *)cameraId;
- (void)netManager:(OCNetManager *)manager didReceiveRemoteFilterState:(OCFilterState *)state;
- (void)netManager:(OCNetManager *)manager didReceiveRemoteSession:(NSDictionary *)state;
- (void)netManager:(OCNetManager *)manager didUpdateStatsFps:(double)fps
                                     kbps:(double)kbps encMs:(double)encMs
                                  lossPct:(double)lossPct nacks:(uint32_t)nacks
                                     sent:(uint32_t)sent;
- (void)netManager:(OCNetManager *)manager didFailWithMessage:(NSString *)message;
@end

@interface OCNetManager : NSObject

@property (nonatomic, weak, nullable) id<OCNetManagerDelegate> delegate;
// Wired by the view controller; all weak — the VC owns the objects.
@property (nonatomic, weak, nullable) OCCaptureEngine *captureEngine;
@property (nonatomic, weak, nullable) OCEncoder *encoder;
@property (nonatomic, weak, nullable) OCPacker *packer;
@property (nonatomic, weak, nullable) OCFilterState *filterState;

@property (nonatomic, readonly, getter=isClientConnected) BOOL clientConnected;
@property (nonatomic, readonly, getter=isStreaming) BOOL streaming;
@property (nonatomic, copy, readonly, nullable) NSString *clientName;
@property (nonatomic, copy, readonly, nullable) NSString *clientAddress; // PC IP (TCP peer)
@property (nonatomic, copy, readonly) NSString *deviceModel;             // sysctl hw.model
@property (nonatomic, readonly) BOOL abrAuto;                            // §6 default true
@property (nonatomic, readonly) int currentBitrateKbps;

/// Creates sockets + beacon timer + TCP listener. Safe to call once at startup.
- (void)start;
- (void)stopAll;

/// Shared core for the PC `start` message and the phone UI Start button.
- (BOOL)startStreamingToAddress:(NSString *)ip
                      videoPort:(int)videoPort
                          width:(int)w
                         height:(int)h
                            fps:(int)fps
                           kbps:(int)kbps
                         keyint:(int)keyint
                          error:(NSError **)error;
/// Phone Start button: same TCP-peer dest as the PC `start` message.
- (BOOL)startStreamingToConnectedClientWidth:(int)w
                                      height:(int)h
                                         fps:(int)fps
                                        kbps:(int)kbps
                                      keyint:(int)keyint
                                       error:(NSError **)error;
- (void)stopStreaming;

/// ABR ceiling updates (resolution change on the phone UI).
- (void)setBitrateCeilingKbps:(int)kbps;

/// Phone-side ABR toggle (mirrors the PC's `abr` message).
- (void)enableAutoBitrate:(BOOL)enable;

/// §2.3 shared session (camera, encode size, fps, bitrate, ABR, torch, zoom).
- (NSDictionary *)sessionDictionary;
- (void)pushSessionState;
- (void)noteLocalCameraId:(NSString *)cid;
- (void)notifyLocalResolutionHD:(BOOL)hd;

/// RTP send path used by OCPacker (video/FEC → video port).
- (void)sendVideoDatagram:(NSData *)data;

/// Sends the current filter state to the PC (called on filter changes).
- (void)pushFilterState;

/// Wi-Fi IPv4 of interface en0/en1 (for the UI), or nil.
+ (nullable NSString *)localIPv4;

@end

NS_ASSUME_NONNULL_END
