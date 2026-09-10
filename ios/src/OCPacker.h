//
//  OCPacker.h — RTP packer/depacker per PROTOCOL.md §3.
//
//  Video: PT 96, 90 kHz ts, seq16, random SSRC. Single NALU ≤1200 B, FU-A above,
//  STAP-A (SPS+PPS) as first packet of every IDR access unit, marker bit on the
//  last packet of a frame, max payload 1200 B. Retransmit ring cache (2048 datagrams,
//  evicted after 2 s) answers RFC 4585 NACKs; PLI forces an encoder keyframe.
//  FEC (optional, default off): PT 100, own SSRC + own seq, XOR groups of 4 packets.
#import <Foundation/Foundation.h>
#import "OCEncoder.h"

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(uint8_t, OCRTPPayloadType) {
    OCRTPPayloadTypeVideo = 96, // PROTOCOL.md §0
    OCRTPPayloadTypeFEC   = 100,
};

FOUNDATION_EXPORT const uint32_t OCRTPMaxVideoPayload; // 1200
FOUNDATION_EXPORT const uint32_t OCRTPClockVideo;     // 90000
FOUNDATION_EXPORT const NSUInteger OCNackCacheSlots;  // 2048

@class OCNetManager;

@interface OCPacker : NSObject <OCEncoderDelegate>

/// UDP transport (video/FEC send). Weak; owned by the view controller.
@property (nonatomic, weak, nullable) OCNetManager *network;
/// PLI answers with a forced keyframe. Weak.
@property (nonatomic, weak, nullable) OCEncoder *encoder;

@property (nonatomic, readonly) uint32_t ssrcVideo;
@property (nonatomic, readonly) uint32_t ssrcFEC;
@property (nonatomic, readonly, getter=isFecEnabled) BOOL fecEnabled;

/// Randomizes SSRCs/seqs and clears the retransmit cache. Call when a stream starts.
- (void)beginStream;
- (void)endStream;
- (void)setFecEnabled:(BOOL)on;

/// Incoming RTCP feedback bytes from the UDP recv path (NACK PT=205 FMT=1, PLI PT=206 FMT=1).
/// Malformed packets are ignored. Call on a serial queue.
- (void)processFeedbackBytes:(const uint8_t * _Nonnull)bytes length:(NSUInteger)length;

// Monotonic counters (atomic) for the 1 Hz `stats` message deltas.
- (uint32_t)totalVideoFrames;
- (uint32_t)totalSentPackets;     // video datagrams incl. retransmits
- (uint32_t)totalRetransmitted;   // datagrams re-sent on NACK
- (uint64_t)totalSentBytes;       // video + FEC payload bytes

@end

NS_ASSUME_NONNULL_END
