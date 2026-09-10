//
//  OCPacker.m — RFC 6184 packetization, retransmit ring, NACK/PLI, optional XOR FEC.
//
#import "OCPacker.h"
#import "OCNetManager.h"

#import <mach/mach_time.h>
#import <arpa/inet.h>
#import <stdlib.h>

const uint32_t OCRTPMaxVideoPayload = 1200;
const uint32_t OCRTPClockVideo = 90000;
const NSUInteger OCNackCacheSlots = 2048;

static const uint64_t OCCacheMaxAgeMs = 2000;   // §3.2: ≥ 2 s of datagrams
static const uint64_t OCResendMinGapMs = 10;    // flood guard for repeated NACKs
static const int OCFecGroupSize = 4;            // §3.4

#define OC_ATOMIC_ADD(v, d) __atomic_add_fetch(&(v), (d), __ATOMIC_RELAXED)
#define OC_ATOMIC_LOAD(v)   __atomic_load_n(&(v), __ATOMIC_RELAXED)

static uint64_t ocNowMs(void) {
    static mach_timebase_info_data_t tb;
    if (tb.denom == 0) mach_timebase_info(&tb);
    return mach_absolute_time() * tb.numer / tb.denom / 1000000ull;
}

typedef struct {
    NSData * __strong datagram;
    uint64_t addedMs;
    uint64_t lastResendMs;
} OCCacheSlot;

typedef struct {
    uint16_t seq;
    uint32_t ts;
    NSData * __strong payload;
} OCFecMember;

@interface OCPacker () {
    OCCacheSlot _cache[2048];
    OCFecMember _fecGroup[4];
    int _fecCount;
    uint32_t _framesTotal;
    uint32_t _sentTotal;
    uint32_t _resentTotal;
    uint64_t _bytesTotal;
}
@property (nonatomic, assign) uint32_t ssrcVideo;
@property (nonatomic, assign) uint32_t ssrcFEC;
@property (nonatomic, assign) uint16_t seqVideo;
@property (nonatomic, assign) uint16_t seqFEC;
@property (nonatomic, assign) BOOL fecEnabled;
@property (nonatomic, copy, nullable) NSData *sps;
@property (nonatomic, copy, nullable) NSData *pps;
@property (nonatomic, strong) NSLock *cacheLock; // cache + FEC group + seq counters
@end

@implementation OCPacker

// Explicit synthesis: the readonly header declaration + readwrite extension
// redeclaration pattern doesn't reliably auto-synthesize on this toolchain.
@synthesize fecEnabled = _fecEnabled;

- (instancetype)init {
    self = [super init];
    if (!self) return nil;
    _cacheLock = [[NSLock alloc] init];
    [self beginStream];
    return self;
}

- (void)beginStream {
    [_cacheLock lock];
    _ssrcVideo = arc4random(); if (_ssrcVideo == 0) _ssrcVideo = 1;
    _ssrcFEC   = arc4random(); if (_ssrcFEC == 0 || _ssrcFEC == _ssrcVideo) _ssrcFEC = _ssrcVideo + 2;
    _seqVideo = (uint16_t)(arc4random() & 0x7FFF);
    _seqFEC   = (uint16_t)(arc4random() & 0x7FFF);
    for (NSUInteger i = 0; i < OCNackCacheSlots; i++) {
        _cache[i].datagram = nil;
        _cache[i].addedMs = 0;
        _cache[i].lastResendMs = 0;
    }
    [self clearFecGroupLocked];
    [_cacheLock unlock];
}

- (void)endStream {
    [_cacheLock lock];
    for (NSUInteger i = 0; i < OCNackCacheSlots; i++) {
        _cache[i].datagram = nil;
    }
    [self clearFecGroupLocked];
    [_cacheLock unlock];
}

- (void)setFecEnabled:(BOOL)on {
    [_cacheLock lock];
    if (_fecEnabled != on) {
        _fecEnabled = on;
        [self clearFecGroupLocked];
        NSLog(@"[OmniCam] FEC %@", on ? @"enabled" : @"disabled");
    }
    [_cacheLock unlock];
}

- (BOOL)isFecEnabled { return _fecEnabled; }

- (uint32_t)totalVideoFrames { return OC_ATOMIC_LOAD(_framesTotal); }
- (uint32_t)totalSentPackets { return OC_ATOMIC_LOAD(_sentTotal); }
- (uint32_t)totalRetransmitted { return OC_ATOMIC_LOAD(_resentTotal); }
- (uint64_t)totalSentBytes { return OC_ATOMIC_LOAD(_bytesTotal); }

#pragma mark RTP header helpers

static NSMutableData *ocRtpPacket(uint8_t pt, BOOL marker, uint16_t seq, uint32_t ts, uint32_t ssrc,
                                  const uint8_t *payload, NSUInteger len) {
    NSMutableData *d = [NSMutableData dataWithLength:12 + len];
    uint8_t *b = (uint8_t *)d.mutableBytes;
    b[0] = 0x80;                                  // V=2, P=0, X=0, CC=0
    b[1] = (uint8_t)((marker ? 0x80 : 0x00) | (pt & 0x7F));
    b[2] = (uint8_t)(seq >> 8);  b[3] = (uint8_t)(seq & 0xFF);
    b[4] = (uint8_t)(ts >> 24);  b[5] = (uint8_t)(ts >> 16);  b[6] = (uint8_t)(ts >> 8);  b[7] = (uint8_t)ts;
    b[8] = (uint8_t)(ssrc >> 24); b[9] = (uint8_t)(ssrc >> 16); b[10] = (uint8_t)(ssrc >> 8); b[11] = (uint8_t)ssrc;
    if (len) memcpy(b + 12, payload, len);
    return d;
}

static inline void ocSetMarker(NSMutableData *pkt) {
    ((uint8_t *)pkt.mutableBytes)[1] |= 0x80;
}

#pragma mark OCEncoderDelegate

- (void)encoder:(OCEncoder *)encoder didUpdateParameterSetsSPS:(NSData *)sps PPS:(NSData *)pps {
    [_cacheLock lock];
    _sps = sps;
    _pps = pps;
    [_cacheLock unlock];
}

- (void)encoder:(OCEncoder *)encoder
 didProduceAnnexB:(NSData *)annexB
           isIDR:(BOOL)isIDR
           ptsMs:(double)ptsMs
           dtsMs:(double)dtsMs {
    OCNetManager *net = _network;
    if (!net || annexB.length == 0) return;

    uint32_t ts = (uint32_t)(uint64_t)llround(ptsMs * (OCRTPClockVideo / 1000.0));
    const uint8_t *bytes = annexB.bytes;
    NSUInteger len = annexB.length;

    // Split Annex-B on 00 00 00 01 (our encoder emits exactly that form).
    NSMutableArray<NSValue *> *nalus = [NSMutableArray array];
    NSUInteger i = 0;
    while (i + 4 <= len) {
        if (bytes[i] == 0 && bytes[i + 1] == 0 && bytes[i + 2] == 0 && bytes[i + 3] == 1) {
            NSUInteger start = i + 4;
            NSUInteger j = start;
            NSUInteger end = len;
            while (j + 4 <= len) {
                if (bytes[j] == 0 && bytes[j + 1] == 0 && bytes[j + 2] == 0 && bytes[j + 3] == 1) {
                    end = j;
                    break;
                }
                j++;
            }
            if (end > start) [nalus addObject:[NSValue valueWithRange:NSMakeRange(start, end - start)]];
            i = end;
        } else {
            i++;
        }
    }
    if (nalus.count == 0) return;

    NSMutableArray<NSMutableData *> *packets = [NSMutableArray arrayWithCapacity:8];

    [_cacheLock lock];

    // §3.1: every IDR access unit starts with a STAP-A carrying SPS then PPS.
    if (isIDR && _sps.length > 0 && _pps.length > 0) {
        NSMutableData *stap = [NSMutableData dataWithCapacity:_sps.length + _pps.length + 5];
        uint8_t nriS = (((const uint8_t *)_sps.bytes)[0] >> 5) & 0x3;
        uint8_t nriP = (((const uint8_t *)_pps.bytes)[0] >> 5) & 0x3;
        uint8_t nri = MAX(nriS, nriP);
        uint8_t hdr = (uint8_t)((nri << 5) | 24);
        [stap appendBytes:&hdr length:1];
        uint16_t ls = htons((uint16_t)_sps.length);
        [stap appendBytes:&ls length:2];
        [stap appendData:_sps];
        uint16_t lp = htons((uint16_t)_pps.length);
        [stap appendBytes:&lp length:2];
        [stap appendData:_pps];
        [packets addObject:ocRtpPacket(OCRTPPayloadTypeVideo, NO, _seqVideo++, ts, _ssrcVideo, stap.bytes, stap.length)];
    }

    for (NSValue *v in nalus) {
        NSRange r = v.rangeValue;
        const uint8_t *nal = bytes + r.location;
        NSUInteger nlen = r.length;
        if (nlen <= OCRTPMaxVideoPayload) {
            [packets addObject:ocRtpPacket(OCRTPPayloadTypeVideo, NO, _seqVideo++, ts, _ssrcVideo, nal, nlen)];
        } else {
            // FU-A (type 28): indicator = (NRI<<5)|28, FU header = S/E bits + original type.
            uint8_t indicator = (uint8_t)((nal[0] & 0x60) | 28);
            uint8_t type = nal[0] & 0x1F;
            NSUInteger pos = 1; // skip original NAL header byte
            NSUInteger chunk = OCRTPMaxVideoPayload - 2;
            BOOL first = YES;
            while (pos < nlen) {
                NSUInteger take = MIN(chunk, nlen - pos);
                BOOL last = (pos + take) >= nlen;
                uint8_t fuHdr = (uint8_t)((first ? 0x80 : 0x00) | (last ? 0x40 : 0x00) | type);
                NSMutableData *payload = [NSMutableData dataWithCapacity:take + 2];
                [payload appendBytes:&indicator length:1];
                [payload appendBytes:&fuHdr length:1];
                [payload appendBytes:nal + pos length:take];
                [packets addObject:ocRtpPacket(OCRTPPayloadTypeVideo, NO, _seqVideo++, ts, _ssrcVideo,
                                               payload.bytes, payload.length)];
                pos += take;
                first = NO;
            }
        }
    }
    ocSetMarker(packets.lastObject); // marker on the final packet of the frame

    for (NSMutableData *pkt in packets) {
        [self cacheAndSendLocked:pkt via:net];
    }
    [_cacheLock unlock];

    OC_ATOMIC_ADD(_framesTotal, 1);
}

// Caller holds _cacheLock.
- (void)cacheAndSendLocked:(NSData *)pkt via:(OCNetManager *)net {
    const uint8_t *b = pkt.bytes;
    uint16_t seq = (uint16_t)((b[2] << 8) | b[3]);
    uint32_t ts = ((uint32_t)b[4] << 24) | ((uint32_t)b[5] << 16) | ((uint32_t)b[6] << 8) | b[7];

    OCCacheSlot *slot = &_cache[seq % OCNackCacheSlots];
    slot->datagram = pkt;
    slot->addedMs = ocNowMs();
    slot->lastResendMs = 0;

    [net sendVideoDatagram:pkt];
    OC_ATOMIC_ADD(_sentTotal, 1);
    OC_ATOMIC_ADD(_bytesTotal, (uint64_t)pkt.length);

    if (_fecEnabled) {
        NSData *payload = [pkt subdataWithRange:NSMakeRange(12, pkt.length - 12)];
        _fecGroup[_fecCount].seq = seq;
        _fecGroup[_fecCount].ts = ts;
        _fecGroup[_fecCount].payload = payload;
        _fecCount++;
        if (_fecCount == OCFecGroupSize) {
            [self emitFecLocked:net];
        }
    }
}

// §3.4: [base_seq u16][count u8][flags u8][XOR of payloads zero-padded to max length].
- (void)emitFecLocked:(OCNetManager *)net {
    NSUInteger maxLen = 0;
    for (int i = 0; i < _fecCount; i++) maxLen = MAX(maxLen, _fecGroup[i].payload.length);
    NSMutableData *payload = [NSMutableData dataWithLength:4 + maxLen];
    uint8_t *p = (uint8_t *)payload.mutableBytes;
    uint16_t base = htons(_fecGroup[0].seq);
    memcpy(p, &base, 2);
    p[2] = (uint8_t)_fecCount;
    p[3] = 0; // flags
    uint8_t *x = p + 4;
    for (int i = 0; i < _fecCount; i++) {
        const uint8_t *src = _fecGroup[i].payload.bytes;
        NSUInteger n = _fecGroup[i].payload.length;
        for (NSUInteger k = 0; k < n; k++) x[k] ^= src[k];
    }
    uint32_t ts = _fecGroup[_fecCount - 1].ts; // ts of last covered packet
    NSMutableData *pkt = ocRtpPacket(OCRTPPayloadTypeFEC, NO, _seqFEC++, ts, _ssrcFEC, payload.bytes, payload.length);
    [net sendVideoDatagram:pkt];
    OC_ATOMIC_ADD(_bytesTotal, (uint64_t)pkt.length);
    [self clearFecGroupLocked];
}

- (void)clearFecGroupLocked {
    for (int i = 0; i < OCFecGroupSize; i++) {
        _fecGroup[i].payload = nil;
        _fecGroup[i].seq = 0;
        _fecGroup[i].ts = 0;
    }
    _fecCount = 0;
}

#pragma mark Feedback (§3.3)

- (void)processFeedbackBytes:(const uint8_t *)bytes length:(NSUInteger)length {
    if (!bytes || length < 12) return;
    uint8_t version = bytes[0] >> 6;
    if (version != 2) return;
    uint8_t fmt = bytes[0] & 0x1F;
    uint8_t pt = bytes[1];
    uint32_t mediaSsrc = ((uint32_t)bytes[8] << 24) | ((uint32_t)bytes[9] << 16) |
                         ((uint32_t)bytes[10] << 8) | bytes[11];

    if (pt == 205 && fmt == 1) {                 // Generic NACK
        if (mediaSsrc != _ssrcVideo) return;
        NSUInteger entries = (length - 12) / 4;
        if (entries > 16) entries = 16;          // §3.3 cap
        OCNetManager *net = _network;
        if (!net) return;
        [_cacheLock lock];
        for (NSUInteger e = 0; e < entries; e++) {
            const uint8_t *p = bytes + 12 + e * 4;
            uint16_t pid = (uint16_t)((p[0] << 8) | p[1]);
            uint16_t blp = (uint16_t)((p[2] << 8) | p[3]);
            [self resendSeqLocked:pid via:net];
            for (int bit = 0; bit < 16; bit++) {
                if (blp & (1u << bit)) {
                    [self resendSeqLocked:(uint16_t)(pid + 1 + bit) via:net];
                }
            }
        }
        [_cacheLock unlock];
    } else if (pt == 206 && fmt == 1) {          // PLI → force IDR
        if (mediaSsrc != _ssrcVideo) return;
        OCEncoder *enc = _encoder;
        if (enc) {
            [enc forceKeyframe];
            NSLog(@"[OmniCam] PLI received → IDR");
        }
    }
    // Any other RTCP is ignored (malformed or not ours).
}

// Caller holds _cacheLock. Re-sends the original bytes unchanged (§3.2).
- (void)resendSeqLocked:(uint16_t)seq via:(OCNetManager *)net {
    OCCacheSlot *slot = &_cache[seq % OCNackCacheSlots];
    NSData *dg = slot->datagram;
    if (!dg || dg.length < 12) return;
    const uint8_t *b = dg.bytes;
    uint16_t cachedSeq = (uint16_t)((b[2] << 8) | b[3]);
    if (cachedSeq != seq) return; // slot recycled by a newer seq
    uint64_t now = ocNowMs();
    if (now - slot->addedMs > OCCacheMaxAgeMs) {
        slot->datagram = nil; // evict >2 s
        return;
    }
    if (slot->lastResendMs && now - slot->lastResendMs < OCResendMinGapMs) return;
    slot->lastResendMs = now;
    [net sendVideoDatagram:dg];
    OC_ATOMIC_ADD(_sentTotal, 1);
    OC_ATOMIC_ADD(_resentTotal, 1);
    OC_ATOMIC_ADD(_bytesTotal, (uint64_t)dg.length);
}

@end
