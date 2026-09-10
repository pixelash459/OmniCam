//
//  OCNetManager.m — BSD POSIX sockets + GCD: beacon, RTP UDP, TCP JSON control, ABR.
//
#import "OCNetManager.h"
#import "OCCaptureEngine.h"
#import "OCEncoder.h"
#import "OCPacker.h"

#import <UIKit/UIKit.h>
#import <arpa/inet.h>
#import <netinet/in.h>
#import <netinet/tcp.h>
#import <sys/socket.h>
#import <sys/sysctl.h>
#import <ifaddrs.h>
#import <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <stdlib.h>

const int OCBeaconPort  = 9920;
const int OCVideoPort   = 9921;
const int OCControlPort = 9923;

NSString * const OCMagicString      = @"OMNICAM1";
NSString * const OCAppVersionString = @"1.1.3";

static const NSUInteger OCMaxLineBytes = 64 * 1024; // §2 max message 64 KiB
static const double OCAbrFloorKbps = 500.0;         // §6

#define OC_ATOMIC_ADD(v, d) __atomic_add_fetch(&(v), (d), __ATOMIC_RELAXED)
#define OC_ATOMIC_LOAD(v)   __atomic_load_n(&(v), __ATOMIC_RELAXED)

static NSString *ocDeviceModel(void) {
    size_t size = 0;
    sysctlbyname("hw.model", NULL, &size, NULL, 0);
    if (size == 0) return @"unknown";
    char *model = (char *)malloc(size);
    if (!model) return @"unknown";
    sysctlbyname("hw.model", model, &size, NULL, 0);
    NSString *s = [NSString stringWithUTF8String:model];
    free(model);
    return s ?: @"unknown";
}

@interface OCNetManager () {
    NSString *_deviceModel;
    NSLock *_streamLock;
    int _udpSock;            // RTP send + NACK/PLI recv, bound locally to 9921
    int _beaconSock;         // SO_BROADCAST, ephemeral port
    int _listenSock;         // TCP 9923
    int _clientSock;         // active control client, -1 = none
    struct sockaddr_in _rtpDest;
    BOOL _destValid;

    // stream config
    BOOL _streaming;
    int _videoPort;
    int _maxBitrateKbps;     // ABR ceiling = configured start bitrate (§6)
    int _currentBitrateKbps;
    int _fps;
    int _streamWidth, _streamHeight; // last start dims — skip stop/start flicker
    double _lastLossPct, _lastJitterMs;
    int _highLossCount;      // consecutive rr with loss > 10 % (sustained-loss IDR)
    int _fecHighCount, _fecLowCount;
    BOOL _applyingRemoteFilter; // suppress echoing a PC-pushed state back

    // stats deltas
    uint32_t _lastFrames, _lastSent, _lastResent;
    uint64_t _lastBytes;
}
@property (nonatomic, strong) dispatch_queue_t udpQueue;
@property (nonatomic, strong) dispatch_queue_t listenQueue;
@property (nonatomic, strong) dispatch_queue_t clientQueue;
@property (nonatomic, strong) dispatch_queue_t beaconQueue;
@property (nonatomic, strong, nullable) dispatch_source_t udpSource;
@property (nonatomic, strong, nullable) dispatch_source_t listenSource;
@property (nonatomic, strong, nullable) dispatch_source_t clientSource;
@property (nonatomic, strong, nullable) dispatch_source_t beaconTimer;
@property (nonatomic, strong, nullable) dispatch_source_t statsTimer;
@property (nonatomic, strong, nullable) NSMutableData *lineBuf;
@property (nonatomic, copy, nullable) NSString *clientName;
@property (nonatomic, copy, nullable) NSString *clientAddress;
@property (nonatomic, assign) BOOL abrAuto;
@property (nonatomic, copy) NSString *deviceModel;
@end

@implementation OCNetManager

- (instancetype)init {
    self = [super init];
    if (!self) return nil;
    _deviceModel = ocDeviceModel();
    _streamLock = [[NSLock alloc] init];
    _udpSock = -1;
    _beaconSock = -1;
    _listenSock = -1;
    _clientSock = -1;
    _videoPort = OCVideoPort;
    _maxBitrateKbps = 3000;
    _currentBitrateKbps = 3000;
    _fps = 30;
    _abrAuto = YES; // §6 default
    _udpQueue = dispatch_queue_create("oc.net.udp", DISPATCH_QUEUE_SERIAL);
    _listenQueue = dispatch_queue_create("oc.net.listen", DISPATCH_QUEUE_SERIAL);
    _clientQueue = dispatch_queue_create("oc.net.client", DISPATCH_QUEUE_SERIAL);
    _beaconQueue = dispatch_queue_create("oc.net.beacon", DISPATCH_QUEUE_SERIAL);
    return self;
}

- (void)dealloc {
    [[NSNotificationCenter defaultCenter] removeObserver:self];
    // No delegate callbacks here (blocks would retain a deallocating self).
    // Cancel sources; their cancel handlers close the fds on the owning queues.
    _streaming = NO;
    dispatch_source_cancel(_udpSource);
    dispatch_source_cancel(_listenSource);
    dispatch_source_cancel(_clientSource);
    dispatch_source_cancel(_beaconTimer);
    dispatch_source_cancel(_statsTimer);
}

- (NSString *)deviceModel { return _deviceModel; }
- (BOOL)isClientConnected { return _clientSock != -1; }
- (BOOL)isStreaming { return _streaming; }
- (NSString *)clientName { return _clientName; }
- (NSString *)clientAddress { return _clientAddress; }
- (BOOL)abrAuto { return _abrAuto; }
- (int)currentBitrateKbps { return _currentBitrateKbps; }

- (void)setPacker:(OCPacker *)packer {
    _packer = packer;
    if (packer) {
        packer.network = self;
        if (_encoder) packer.encoder = _encoder;
    }
}

- (void)setEncoder:(OCEncoder *)encoder {
    _encoder = encoder;
    if (_packer) _packer.encoder = encoder;
}

#pragma mark Start / stop

- (void)start {
    [self setupUdp];
    [self setupTcpListener];
    [self setupBeacon];
    [self setupStatsTimer];

    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(filterStateDidChange:)
                                                 name:OCFilterStateDidChangeNotification
                                               object:nil];
}

- (void)stopAll {
    [self stopStreaming];
    dispatch_async(_clientQueue, ^{
        [self closeClientSocket];
    });
    dispatch_async(_listenQueue, ^{
        if (self->_listenSock != -1) {
            dispatch_source_cancel(self->_listenSource);
            self->_listenSock = -1;
        }
    });
    dispatch_async(_udpQueue, ^{
        if (self->_udpSock != -1) {
            dispatch_source_cancel(self->_udpSource);
            self->_udpSock = -1;
        }
    });
    dispatch_async(_beaconQueue, ^{
        dispatch_source_cancel(self->_beaconTimer);
        if (self->_beaconSock != -1) {
            close(self->_beaconSock);
            self->_beaconSock = -1;
        }
    });
}

+ (nullable NSString *)localIPv4 {
    NSString *result = nil;
    struct ifaddrs *addrs = NULL;
    if (getifaddrs(&addrs) != 0) return nil;
    for (struct ifaddrs *ifa = addrs; ifa; ifa = ifa->ifa_next) {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET) continue;
        NSString *name = [NSString stringWithUTF8String:ifa->ifa_name];
        if (![name hasPrefix:@"en"]) continue;
        char ip[INET_ADDRSTRLEN] = {0};
        struct sockaddr_in *sa = (struct sockaddr_in *)ifa->ifa_addr;
        if (inet_ntop(AF_INET, &sa->sin_addr, ip, sizeof ip)) {
            result = [NSString stringWithUTF8String:ip];
            if ([name isEqualToString:@"en0"]) break; // prefer en0
        }
    }
    freeifaddrs(addrs);
    return result;
}

#pragma mark UDP (RTP + feedback)

- (void)setupUdp {
    dispatch_async(_udpQueue, ^{
        int s = socket(AF_INET, SOCK_DGRAM, 0);
        if (s < 0) {
            [self failOnMain:[NSString stringWithFormat:@"UDP socket() failed: %s", strerror(errno)]];
            return;
        }
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof addr);
        addr.sin_family = AF_INET;
        addr.sin_port = htons(OCVideoPort); // PC sends NACK/PLI back to phone_ip:9921 (§3.3)
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(s, (struct sockaddr *)&addr, sizeof addr) != 0) {
            NSLog(@"[OmniCam] UDP bind :%d failed: %s", OCVideoPort, strerror(errno));
            [self failOnMain:[NSString stringWithFormat:@"UDP bind :%d failed (%s)", OCVideoPort, strerror(errno)]];
            close(s);
            return;
        }
        int rcv = 512 * 1024;
        setsockopt(s, SOL_SOCKET, SO_RCVBUF, &rcv, sizeof rcv);
        int snd = 1024 * 1024;
        setsockopt(s, SOL_SOCKET, SO_SNDBUF, &snd, sizeof snd);

        self->_udpSock = s;
        int fd = s;
        __weak typeof(self) wself = self; // break source→handler→self cycle
        dispatch_source_t src = dispatch_source_create(DISPATCH_SOURCE_TYPE_READ, (uintptr_t)fd, 0, _udpQueue);
        dispatch_source_set_event_handler(src, ^{
            __strong typeof(self) sself = wself;
            if (!sself) return;
            uint8_t buf[2048];
            struct sockaddr_in from;
            socklen_t flen = sizeof from;
            for (int i = 0; i < 16; i++) {
                ssize_t n = recvfrom(fd, buf, sizeof buf, 0, (struct sockaddr *)&from, &flen);
                if (n <= 0) break;
                OCPacker *p = sself->_packer;
                if (p) [p processFeedbackBytes:buf length:(NSUInteger)n];
            }
        });
        dispatch_source_set_cancel_handler(src, ^{
            close(fd);
        });
        self->_udpSource = src;
        dispatch_resume(src);
        NSLog(@"[OmniCam] UDP socket bound :%d", OCVideoPort);
    });
}

- (void)sendVideoDatagram:(NSData *)data { [self sendRtp:data port:_videoPort]; }

- (void)sendRtp:(NSData *)data port:(int)port {
    if (_udpSock == -1 || !_destValid || data.length == 0) return;
    struct sockaddr_in dest = _rtpDest;
    dest.sin_port = htons((uint16_t)port);
    ssize_t n = sendto(_udpSock, data.bytes, data.length, 0, (struct sockaddr *)&dest, sizeof dest);
    if (n < 0 && errno != EAGAIN && errno != EINTR) {
        // Transient Wi-Fi hiccups are expected; log throttled-ish (once per error change).
        static int lastErr = 0;
        if (lastErr != errno) {
            NSLog(@"[OmniCam] sendto failed: %s", strerror(errno));
            lastErr = errno;
        }
    }
}

#pragma mark Beacon (§1)

- (void)setupBeacon {
    dispatch_async(_beaconQueue, ^{
        int s = socket(AF_INET, SOCK_DGRAM, 0);
        if (s < 0) return;
        int on = 1;
        setsockopt(s, SOL_SOCKET, SO_BROADCAST, &on, sizeof on);
        self->_beaconSock = s;

        struct sockaddr_in dest;
        memset(&dest, 0, sizeof dest);
        dest.sin_family = AF_INET;
        dest.sin_port = htons(OCBeaconPort);
        dest.sin_addr.s_addr = inet_addr("255.255.255.255");

        dispatch_source_t t = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, _beaconQueue);
        dispatch_source_set_timer(t, DISPATCH_TIME_NOW, 1.0 * NSEC_PER_SEC, 100 * NSEC_PER_MSEC);
        __weak typeof(self) wself = self; // break timer→handler→self cycle
        dispatch_source_set_event_handler(t, ^{
            __strong typeof(self) sself = wself;
            if (!sself) return;
            NSDictionary *json = @{
                @"magic": OCMagicString,
                @"ver": @1,
                @"name": [[UIDevice currentDevice] name] ?: @"iPhone",
                @"model": sself->_deviceModel,
                @"tcp_port": @(OCControlPort),
                @"streaming": @(sself->_streaming),
                @"app": OCAppVersionString,
            };
            NSData *line = [sself jsonLine:json];
            if (line && sself->_beaconSock != -1) {
                sendto(sself->_beaconSock, line.bytes, line.length, 0,
                       (struct sockaddr *)&dest, sizeof dest);
            }
        });
        self->_beaconTimer = t;
        dispatch_resume(t);
    });
}

#pragma mark TCP control server (§2)

- (void)setupTcpListener {
    dispatch_async(_listenQueue, ^{
        int s = socket(AF_INET, SOCK_STREAM, 0);
        if (s < 0) {
            [self failOnMain:[NSString stringWithFormat:@"TCP socket() failed: %s", strerror(errno)]];
            return;
        }
        int reuse = 1;
        setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof reuse);
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof addr);
        addr.sin_family = AF_INET;
        addr.sin_port = htons(OCControlPort);
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
        if (bind(s, (struct sockaddr *)&addr, sizeof addr) != 0 || listen(s, 8) != 0) {
            [self failOnMain:[NSString stringWithFormat:@"TCP listen :%d failed (%s)", OCControlPort, strerror(errno)]];
            close(s);
            return;
        }
        self->_listenSock = s;
        int fd = s;
        __weak typeof(self) wself = self; // break source→handler→self cycle
        dispatch_source_t src = dispatch_source_create(DISPATCH_SOURCE_TYPE_READ, (uintptr_t)fd, 0, _listenQueue);
        dispatch_source_set_event_handler(src, ^{
            __strong typeof(self) sself = wself;
            if (!sself) return;
            int c = (int)accept(fd, NULL, NULL);
            if (c < 0) return;
            dispatch_async(sself->_clientQueue, ^{
                [sself acceptClientSocket:c];
            });
        });
        self->_listenSource = src;
        dispatch_resume(src);
        NSLog(@"[OmniCam] TCP control listening :%d", OCControlPort);
    });
}

// Runs on _clientQueue.
- (void)acceptClientSocket:(int)c {
    if (_clientSock != -1) {
        // §2: one client at a time — refuse the second with {"t":"error","code":"busy"}.
        const char *busy = "{\"t\":\"error\",\"code\":\"busy\"}\n";
        send(c, busy, strlen(busy), 0);
        close(c);
        return;
    }
    int nodelay = 1;
    setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof nodelay);
    int keepalive = 1;
    setsockopt(c, SOL_SOCKET, SO_KEEPALIVE, &keepalive, sizeof keepalive);
    // GCD DISPATCH_SOURCE_TYPE_READ + blocking recv can spin-close the
    // socket (EAGAIN/0-byte wakes). Non-blocking: drain and return.
    int flags = fcntl(c, F_GETFL, 0);
    if (flags >= 0) fcntl(c, F_SETFL, flags | O_NONBLOCK);

    struct sockaddr_in peer;
    socklen_t plen = sizeof peer;
    char ip[INET_ADDRSTRLEN] = {0};
    if (getpeername(c, (struct sockaddr *)&peer, &plen) == 0 &&
        peer.sin_family == AF_INET &&
        inet_ntop(AF_INET, &peer.sin_addr, ip, sizeof ip)) {
        _clientAddress = [NSString stringWithUTF8String:ip];
    } else {
        _clientAddress = nil;
    }
    _clientSock = c;
    _lineBuf = [NSMutableData data];
    _clientName = nil;

    dispatch_source_t src = dispatch_source_create(DISPATCH_SOURCE_TYPE_READ, (uintptr_t)c, 0, _clientQueue);
    __weak typeof(self) wself = self; // break source→handler→self cycle
    dispatch_source_set_event_handler(src, ^{
        __strong typeof(self) sself = wself;
        if (!sself) return;
        [sself readClientSocket];
    });
    dispatch_source_set_cancel_handler(src, ^{
        // Socket is closed in closeClientSocket to avoid a half-open race.
    });
    _clientSource = src;
    dispatch_resume(src);
    NSLog(@"[OmniCam] PC connected: %@", _clientAddress);
}

// Runs on _clientQueue.
- (void)readClientSocket {
    uint8_t buf[4096];
    for (int i = 0; i < 16; i++) {
        ssize_t n = recv(_clientSock, buf, sizeof buf, 0);
        if (n == 0) {
            [self closeClientSocket];
            return;
        }
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) return;
            [self closeClientSocket];
            return;
        }
        [_lineBuf appendBytes:buf length:(NSUInteger)n];
        if (_lineBuf.length > OCMaxLineBytes) {
            [self sendError:@"badmsg"];
            [_lineBuf setLength:0]; // drop oversized garbage, keep the connection
            return;
        }
        // Newline-delimited JSON framing.
        const uint8_t *bytes = _lineBuf.bytes;
        NSUInteger start = 0;
        for (NSUInteger k = 0; k < _lineBuf.length; k++) {
            if (bytes[k] == '\n') {
                NSData *line = [_lineBuf subdataWithRange:NSMakeRange(start, k - start)];
                start = k + 1;
                [self handleLineData:line];
                // A handler (e.g. "bye") may have torn down the client and freed the buffer.
                if (!_lineBuf) return;
                bytes = _lineBuf.bytes;
            }
        }
        if (start > 0) {
            [_lineBuf replaceBytesInRange:NSMakeRange(0, _lineBuf.length - start) withBytes:bytes + start length:_lineBuf.length - start];
        }
        if (n < (ssize_t)sizeof buf) break;
    }
}

// Runs on _clientQueue.
- (void)closeClientSocket {
    // Close the fd immediately so a reconnecting PC cannot race into
    // accept() while the old socket is still half-open (that looks like
    // connect/disconnect strobing on the phone HUD).
    int fd = _clientSock;
    _clientSock = -1;
    if (_clientSource) {
        dispatch_source_cancel(_clientSource);
        _clientSource = nil;
    }
    if (fd != -1) {
        shutdown(fd, SHUT_RDWR);
        close(fd);
        _lineBuf = nil;
        NSString *ip = _clientAddress;
        _clientAddress = nil;
        _clientName = nil;
        if (_streaming) [self stopStreaming]; // PC gone → stop the stream
        id<OCNetManagerDelegate> d = _delegate;
        if (d && [d respondsToSelector:@selector(netManagerClientDidDisconnect:)]) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [d netManagerClientDidDisconnect:self];
            });
        }
        NSLog(@"[OmniCam] PC disconnected (%@)", ip);
    }
}

- (void)sendTcpLine:(NSString *)line {
    dispatch_async(_clientQueue, ^{
        if (self->_clientSock == -1) return;
        NSMutableData *d = [[line dataUsingEncoding:NSUTF8StringEncoding] mutableCopy];
        [d appendBytes:"\n" length:1];
        const uint8_t *p = d.bytes;
        size_t left = d.length;
        while (left > 0) {
            ssize_t n = send(self->_clientSock, p, left, 0);
            if (n <= 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
                    if (errno == EAGAIN || errno == EWOULDBLOCK) usleep(1000);
                    continue;
                }
                [self closeClientSocket];
                return;
            }
            p += n;
            left -= (size_t)n;
        }
    });
}

- (NSData *)jsonLine:(NSDictionary *)json {
    NSError *err = nil;
    NSData *d = [NSJSONSerialization dataWithJSONObject:json options:0 error:&err];
    if (!d) {
        NSLog(@"[OmniCam] JSON serialization failed: %@", err);
        return nil;
    }
    NSMutableData *m = [d mutableCopy];
    [m appendBytes:"\n" length:1];
    return m;
}

- (void)sendJson:(NSDictionary *)json {
    NSData *line = [self jsonLine:json];
    if (!line) return;
    // Always hop to the client queue (FIFO) so replies from UI-thread callers,
    // handlers and timers keep strict wire order.
    dispatch_async(_clientQueue, ^{
        [self sendNow:line];
    });
}

- (void)sendNow:(NSData *)line {
    if (_clientSock == -1) return;
    const uint8_t *p = line.bytes;
    size_t left = line.length;
    while (left > 0) {
        ssize_t n = send(_clientSock, p, left, 0);
        if (n <= 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) usleep(1000);
                continue;
            }
            [self closeClientSocket];
            return;
        }
        p += n;
        left -= (size_t)n;
    }
}

- (void)sendError:(NSString *)code {
    [self sendJson:@{@"t" : @"error", @"code" : code}];
}

- (void)failOnMain:(NSString *)message {
    id<OCNetManagerDelegate> d = _delegate;
    if (d && [d respondsToSelector:@selector(netManager:didFailWithMessage:)]) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [d netManager:self didFailWithMessage:message];
        });
    }
}

#pragma mark Message dispatch (§2.1)

- (void)handleLineData:(NSData *)lineData {
    if (lineData.length == 0) return;
    NSError *err = nil;
    id obj = [NSJSONSerialization JSONObjectWithData:lineData options:0 error:&err];
    if (![obj isKindOfClass:[NSDictionary class]]) {
        [self sendError:@"badmsg"];
        return;
    }
    NSDictionary *m = obj;
    NSString *t = m[@"t"];
    if (![t isKindOfClass:[NSString class]]) {
        [self sendError:@"badmsg"];
        return;
    }
    NSLog(@"[OmniCam] control <- %@", t);

    if ([t isEqualToString:@"hello"]) {
        [self handleMessageHello:m];
    } else if ([t isEqualToString:@"start"]) {
        [self handleMessageStart:m];
    } else if ([t isEqualToString:@"stop"]) {
        [self stopStreaming]; // replies `stopped`
    } else if ([t isEqualToString:@"camera"]) {
        [self handleMessageCamera:m];
    } else if ([t isEqualToString:@"bitrate"]) {
        [self handleMessageBitrate:m];
    } else if ([t isEqualToString:@"abr"]) {
        [self handleMessageAbr:m];
    } else if ([t isEqualToString:@"idr"]) {
        OCEncoder *e = _encoder;
        if (e) [e forceKeyframe];
    } else if ([t isEqualToString:@"filter"]) {
        [self handleMessageFilter:m];
    } else if ([t isEqualToString:@"torch"]) {
        [self handleMessageTorch:m];
    } else if ([t isEqualToString:@"zoom"]) {
        [self handleMessageZoom:m];
    } else if ([t isEqualToString:@"rr"]) {
        [self handleMessageRr:m];
    } else if ([t isEqualToString:@"ping"]) {
        id ts = m[@"ts"];
        [self sendJson:@{@"t" : @"pong", @"ts" : ts ?: @0}]; // echo unchanged (§2.1)
    } else if ([t isEqualToString:@"bye"]) {
        [self closeClientSocket];
    } else {
        [self sendError:@"nosuch"];
    }
}

- (void)handleMessageHello:(NSDictionary *)m {
    NSString *name = [m valueForKey:@"name"];
    _clientName = [name isKindOfClass:[NSString class]] ? name : nil;
    OCCaptureEngine *ce = _captureEngine;
    OCFilterState *fs = _filterState;
    [self sendJson:@{
        @"t" : @"welcome",
        @"ver" : @1,
        @"app" : OCAppVersionString,
        @"device" : _deviceModel,
        @"ios" : [[UIDevice currentDevice] systemVersion],
        @"camera" : (ce ? ce.activeCameraId : @"back"),
        @"max_front" : @[@1280, @720, @30],   // §2.2
        @"max_back" : @[@1920, @1080, @60],
        @"filter" : (fs ? fs.dictionaryRepresentation : @{}) ?: @{},
    }];
    id<OCNetManagerDelegate> d = _delegate;
    if (d && [d respondsToSelector:@selector(netManagerClientDidConnect:name:)]) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [d netManagerClientDidConnect:self name:self->_clientName ?: @""];
        });
    }
}

// IPv4 address of the connected TCP control peer, or nil. Runs on _clientQueue.
// Re-queries getpeername() on the live accepted socket instead of only trusting
// the cached _clientAddress so the value always matches the current connection.
- (nullable NSString *)currentClientPeerIPv4 {
    if (_clientSock == -1) return nil;
    struct sockaddr_in peer;
    memset(&peer, 0, sizeof peer);
    socklen_t plen = sizeof peer;
    char ip[INET_ADDRSTRLEN] = {0};
    if (getpeername(_clientSock, (struct sockaddr *)&peer, &plen) != 0) return nil;
    if (peer.sin_family != AF_INET) return nil;
    if (!inet_ntop(AF_INET, &peer.sin_addr, ip, sizeof ip)) return nil;
    return [NSString stringWithUTF8String:ip];
}

- (void)handleMessageStart:(NSDictionary *)m {
    NSString *host = [m valueForKey:@"rtp_host"];
    NSDictionary *video = [m valueForKey:@"video"];
    if (![host isKindOfClass:[NSString class]] || ![video isKindOfClass:[NSDictionary class]]) {
        [self sendError:@"badmsg"];
        return;
    }
    int vPort = (int)[self numIn:video key:@"port" def:OCVideoPort];
    int w = (int)[self numIn:video key:@"w" def:1280];
    int h = (int)[self numIn:video key:@"h" def:720];
    int fps = (int)[self numIn:video key:@"fps" def:30];
    int kbps = (int)[self numIn:video key:@"kbps" def:3000];
    int keyint = (int)[self numIn:video key:@"keyint" def:60];

    // Media destination: rtp_host is only advisory (PROTOCOL.md §2.1). On
    // multi-homed Windows hosts (VPN/RustDesk/Tailscale, WSL/Docker/Hyper-V
    // adapters, multiple NICs) the PC's guess of its own LAN IP is frequently
    // wrong, so the UDP video would be fired into a dead address while TCP
    // control still works. The TCP peer address of this very control
    // connection is by definition a working route to the PC — prefer it,
    // keep rtp_host as the fallback for protocol compatibility.
    NSString *peerIP = [self currentClientPeerIPv4] ?: _clientAddress;
    NSString *mediaHost = (peerIP.length > 0) ? peerIP : host;
    NSLog(@"[OmniCam] start: media dest = %@ (from %@; rtp_host = %@)",
          mediaHost, (peerIP.length > 0) ? @"TCP peer" : @"rtp_host fallback", host);

    NSError *err = nil;
    BOOL ok = [self startStreamingToAddress:mediaHost videoPort:vPort
                                       width:w height:h fps:fps kbps:kbps
                                      keyint:keyint error:&err];
    if (!ok) {
        NSLog(@"[OmniCam] start failed: %@", err);
        [self sendError:@"badmsg"]; // protocol allows busy|badmsg|nosuch only
        [self failOnMain:err.localizedDescription ?: @"Stream start failed"];
    }
}

- (void)handleMessageCamera:(NSDictionary *)m {
    NSString *idStr = [m valueForKey:@"id"];
    if (![idStr isEqualToString:@"front"] && ![idStr isEqualToString:@"back"]) {
        [self sendError:@"badmsg"];
        return;
    }
    OCCaptureEngine *ce = _captureEngine;
    if (!ce) {
        [self sendError:@"badmsg"];
        return;
    }
    [ce switchToCameraId:idStr completion:^(NSString *activeId, NSError *err) {
        dispatch_async(self->_clientQueue, ^{
            if (!err) {
                [self sendJson:@{@"t" : @"camera_ok", @"id" : activeId}]; // §2.2 after the ~150-300 ms gap
            } else {
                [self sendError:@"badmsg"];
            }
        });
        id<OCNetManagerDelegate> d = self->_delegate;
        if (d && [d respondsToSelector:@selector(netManager:activeCameraDidChange:)]) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [d netManager:self activeCameraDidChange:activeId];
            });
        }
    }];
}

- (void)handleMessageBitrate:(NSDictionary *)m {
    int kbps = (int)[self num:m key:@"kbps" def:0];
    if (kbps <= 0) {
        [self sendError:@"badmsg"];
        return;
    }
    _abrAuto = NO; // §6: manual bitrate disables auto until abr auto=true
    _currentBitrateKbps = kbps;
    OCEncoder *e = _encoder;
    if (e) [e setBitrateKbps:kbps];
    [self sendJson:@{@"t" : @"bitrate_ok", @"kbps" : @(kbps), @"auto" : @NO}];
}

- (void)handleMessageAbr:(NSDictionary *)m {
    _abrAuto = [self flagIn:m key:@"auto" def:YES];
    [self sendJson:@{@"t" : @"bitrate_ok", @"kbps" : @(_currentBitrateKbps), @"auto" : @(_abrAuto)}];
}

- (void)handleMessageFilter:(NSDictionary *)m {
    NSDictionary *state = [m valueForKey:@"state"];
    OCFilterState *fs = _filterState;
    if (![state isKindOfClass:[NSDictionary class]] || !fs) {
        [self sendError:@"badmsg"];
        return;
    }
    _applyingRemoteFilter = YES;
    // Main thread per OCFilterState's concurrency contract (UI sync via delegate).
    dispatch_sync(dispatch_get_main_queue(), ^{
        [fs loadFromDictionary:state];
    });
    _applyingRemoteFilter = NO;
    id<OCNetManagerDelegate> d = _delegate;
    if (d && [d respondsToSelector:@selector(netManager:didReceiveRemoteFilterState:)]) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [d netManager:self didReceiveRemoteFilterState:fs];
        });
    }
    [self sendJson:@{@"t" : @"filter_ok"}];
}

- (void)handleMessageTorch:(NSDictionary *)m {
    BOOL on = [self flagIn:m key:@"on" def:NO];
    OCCaptureEngine *ce = _captureEngine;
    if (!ce) {
        [self sendError:@"badmsg"];
        return;
    }
    BOOL actual = [ce applyTorchOn:on];
    [self sendJson:@{@"t" : @"torch_ok", @"on" : @(actual)}]; // actual state (front has no torch)
}

- (void)handleMessageZoom:(NSDictionary *)m {
    double x = [self num:m key:@"x" def:0];
    OCCaptureEngine *ce = _captureEngine;
    if (x <= 0.0 || !ce) {
        [self sendError:@"badmsg"];
        return;
    }
    [ce setZoomFactor:(CGFloat)x]; // clamped to [1, activeFormat max] (§2.1 "clamped by phone")
}

- (void)handleMessageRr:(NSDictionary *)m {
    _lastLossPct = [self num:m key:@"loss_pct" def:0];
    _lastJitterMs = [self num:m key:@"jitter_ms" def:0];
    if (_streaming && _abrAuto) {
        [self applyAbr];
    }
    [self updateFecDecision];
}

#pragma mark ABR (§6)

- (void)applyAbr {
    double loss = _lastLossPct;
    int before = _currentBitrateKbps;
    if (loss < 2.0) {
        _highLossCount = 0;
        _currentBitrateKbps = (int)MIN((double)_maxBitrateKbps, (double)_currentBitrateKbps * 1.05);
    } else if (loss <= 10.0) {
        _highLossCount = 0; // hold
    } else {
        _highLossCount++;
        double factor = 1.0 - 0.5 * (loss / 100.0);
        _currentBitrateKbps = (int)MAX(OCAbrFloorKbps, (double)_currentBitrateKbps * factor);
        if (_highLossCount >= 3) { // sustained loss → IDR (§6)
            _highLossCount = 0;
            OCEncoder *e = _encoder;
            if (e) [e forceKeyframe];
        }
    }
    if (_currentBitrateKbps != before) {
        OCEncoder *e = _encoder;
        if (e) [e setBitrateKbps:_currentBitrateKbps];
        NSLog(@"[OmniCam] ABR loss %.1f%% → %d kbps", loss, _currentBitrateKbps);
    }
}

// §3.4: FEC on when loss > 2 % for 2 s, off when 0 % for 5 s (rr period = 500 ms).
- (void)updateFecDecision {
    OCPacker *p = _packer;
    if (!p || !_streaming) return;
    double loss = _lastLossPct;
    if (loss > 2.0) {
        _fecHighCount++;
        _fecLowCount = 0;
        if (_fecHighCount >= 4) [p setFecEnabled:YES];
    } else if (loss <= 0.001) {
        _fecLowCount++;
        _fecHighCount = 0;
        if (_fecLowCount >= 10) [p setFecEnabled:NO];
    } else {
        _fecHighCount = 0;
    }
}

#pragma mark Stream control

// Serializes start/stop: the UI (global queue) and the TCP handler (client queue)
// can both drive these.
- (BOOL)startStreamingToAddress:(NSString *)ip
                      videoPort:(int)videoPort
                          width:(int)w
                         height:(int)h
                            fps:(int)fps
                           kbps:(int)kbps
                         keyint:(int)keyint
                          error:(NSError **)error {
    [_streamLock lock];
    BOOL ok = [self startStreamingLockedToAddress:ip videoPort:videoPort
                                            width:w height:h fps:fps kbps:kbps
                                           keyint:keyint error:error];
    [_streamLock unlock];
    return ok;
}

- (BOOL)startStreamingToConnectedClientWidth:(int)w
                                      height:(int)h
                                         fps:(int)fps
                                        kbps:(int)kbps
                                      keyint:(int)keyint
                                       error:(NSError **)error {
    __block NSString *peerIP = nil;
    dispatch_sync(_clientQueue, ^{
        peerIP = [self currentClientPeerIPv4] ?: self->_clientAddress;
    });
    if (peerIP.length == 0) {
        if (error) *error = [NSError errorWithDomain:@"OCNetManager" code:1
                                userInfo:@{NSLocalizedDescriptionKey : @"No TCP client for RTP dest"}];
        return NO;
    }
    NSLog(@"[OmniCam] UI start: media dest = %@ (TCP peer)", peerIP);
    return [self startStreamingToAddress:peerIP videoPort:OCVideoPort
                                   width:w height:h fps:fps kbps:kbps
                                  keyint:keyint error:error];
}

- (BOOL)startStreamingLockedToAddress:(NSString *)ip
                            videoPort:(int)videoPort
                               width:(int)w
                              height:(int)h
                                 fps:(int)fps
                                kbps:(int)kbps
                               keyint:(int)keyint
                              error:(NSError **)error {
    if (ip.length == 0 || kbps <= 0 || fps <= 0) {
        if (error) *error = [NSError errorWithDomain:@"OCNetManager" code:1
                                  userInfo:@{NSLocalizedDescriptionKey : @"Invalid start parameters"}];
        return NO;
    }

    struct sockaddr_in dest;
    memset(&dest, 0, sizeof dest);
    dest.sin_family = AF_INET;
    dest.sin_port = htons((uint16_t)videoPort);
    if (inet_pton(AF_INET, ip.UTF8String, &dest.sin_addr) != 1) {
        if (error) *error = [NSError errorWithDomain:@"OCNetManager" code:2
                                  userInfo:@{NSLocalizedDescriptionKey : [NSString stringWithFormat:@"Bad IP: %@", ip]}];
        return NO;
    }

    // Same PC hitting Start again (or a duplicate `start` after welcome) used
    // to stop+restart the encoder every time — HUD START/STOP strobe and a
    // 1–2 frame black flash. Keep the live session if dest+dims match.
    if (_streaming && _destValid && _videoPort == videoPort
        && _rtpDest.sin_addr.s_addr == dest.sin_addr.s_addr
        && _streamWidth == w && _streamHeight == h && _fps == fps) {
        NSLog(@"[OmniCam] start ignored (already streaming → %@ :%d)", ip, videoPort);
        OCPacker *pk = _packer;
        [self sendJson:@{
            @"t" : @"started",
            @"ssrc_video" : @(pk ? pk.ssrcVideo : 0),
            @"ssrc_fec" : @(pk ? pk.ssrcFEC : 0),
            @"fec" : @(pk ? pk.isFecEnabled : NO),
        }];
        return YES;
    }

    OCCaptureEngine *ce = _captureEngine;
    OCEncoder *enc = _encoder;
    OCPacker *pk = _packer;
    if (!ce || !enc || !pk) {
        if (error) *error = [NSError errorWithDomain:@"OCNetManager" code:3
                                  userInfo:@{NSLocalizedDescriptionKey : @"Engine not wired"}];
        return NO;
    }

    // Already holding _streamLock (startStreamingToAddress) — use the locked variant.
    if (_streaming) [self stopStreamingLocked];

    // Front camera caps at 720p (DECISIONS.md) — clamp silently.
    BOOL isFront = [ce.activeCameraId isEqualToString:@"front"];
    if (isFront && (w > 1280 || h > 720)) {
        w = 1280;
        h = 720;
    }

    // Reset SSRC/seq and set the UDP destination BEFORE any frame can be packed:
    // the encoder starts producing immediately once wired to the pipeline.
    _rtpDest = dest;
    _destValid = YES;
    _videoPort = videoPort;
    [pk beginStream];

    if (![ce isRunning]) {
        [ce setWantsHighResolution:(w > 1280 || h > 720)];
        if (![ce startAndReturnError:error]) return NO;
    }

    if (![enc startWithWidth:w height:h fps:fps bitrateKbps:kbps keyint:keyint error:error]) {
        return NO; // encoder creation failure → log + HUD error via delegate
    }

    _streaming = YES;
    _fps = fps;
    _streamWidth = w;
    _streamHeight = h;
    _maxBitrateKbps = kbps; // ABR cap = configured bitrate (§6)
    _currentBitrateKbps = kbps;
    _highLossCount = 0;
    _fecHighCount = 0;
    _fecLowCount = 0;
    _lastFrames = _lastSent = _lastResent = 0;
    _lastBytes = 0;

    [self sendJson:@{
        @"t" : @"started",
        @"ssrc_video" : @(pk.ssrcVideo),
        @"ssrc_fec" : @(pk.ssrcFEC),
        @"fec" : @NO, // §3.4: FEC starts off; PC detects activation via PT 100
    }];

    id<OCNetManagerDelegate> d = _delegate;
    if (d && [d respondsToSelector:@selector(netManagerStreamingStateDidChange:)]) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [d netManagerStreamingStateDidChange:self];
        });
    }
    NSLog(@"[OmniCam] streaming → %@ (%dx%d@%d, %d kbps)", ip, w, h, fps, kbps);
    return YES;
}

- (void)stopStreaming {
    [_streamLock lock];
    [self stopStreamingLocked];
    [_streamLock unlock];
}

// Caller holds _streamLock.
- (void)stopStreamingLocked {
    if (!_streaming) return;
    _streaming = NO;
    _destValid = NO;
    _streamWidth = _streamHeight = 0;
    OCEncoder *e = _encoder;
    if (e) [e stop];
    OCPacker *pk = _packer;
    if (pk) [pk endStream];
    [self sendJson:@{@"t" : @"stopped"}];

    id<OCNetManagerDelegate> d = _delegate;
    if (d && [d respondsToSelector:@selector(netManagerStreamingStateDidChange:)]) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [d netManagerStreamingStateDidChange:self];
        });
    }
    NSLog(@"[OmniCam] streaming stopped");
}

- (void)setBitrateCeilingKbps:(int)kbps {
    if (kbps <= 0) return;
    _maxBitrateKbps = kbps;
    if (_currentBitrateKbps > kbps) {
        _currentBitrateKbps = kbps;
        OCEncoder *e = _encoder;
        if (e) [e setBitrateKbps:kbps];
    }
}

- (void)enableAutoBitrate:(BOOL)enable {
    _abrAuto = enable;
    OCEncoder *e = _encoder;
    if (!enable && e) [e setBitrateKbps:_currentBitrateKbps];
}

#pragma mark Filter push

- (void)pushFilterState {
    [self sendJson:@{@"t" : @"filter", @"state" : _filterState.dictionaryRepresentation ?: @{}}];
}

- (void)filterStateDidChange:(NSNotification *)note {
    if (note.object != _filterState) return;
    if (_applyingRemoteFilter) return; // don't echo PC-pushed state back
    if (_clientSock == -1) return;
    [self pushFilterState];
}

#pragma mark Stats (§2.2, 1 Hz)

- (void)setupStatsTimer {
    dispatch_source_t t = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, _clientQueue);
    dispatch_source_set_timer(t, DISPATCH_TIME_NOW, 1.0 * NSEC_PER_SEC, 100 * NSEC_PER_MSEC);
    __weak typeof(self) wself = self; // break timer→handler→self cycle
    dispatch_source_set_event_handler(t, ^{
        __strong typeof(self) sself = wself;
        if (!sself) return;
        if (!sself->_streaming || sself->_clientSock == -1) return;
        OCPacker *p = sself->_packer;
        OCEncoder *e = sself->_encoder;
        if (!p) return;
        uint32_t frames = [p totalVideoFrames];
        uint32_t sent = [p totalSentPackets];
        uint32_t resent = [p totalRetransmitted];
        uint64_t bytes = [p totalSentBytes];
        uint32_t dFrames = frames - sself->_lastFrames;
        uint32_t dSent = sent - sself->_lastSent;
        uint32_t dResent = resent - sself->_lastResent;
        uint64_t dBytes = bytes - sself->_lastBytes;
        sself->_lastFrames = frames;
        sself->_lastSent = sent;
        sself->_lastResent = resent;
        sself->_lastBytes = bytes;
        double kbps = (double)dBytes * 8.0 / 1000.0;
        double fpsNow = (double)dFrames;
        double encMs = e ? e.lastEncodeDurationMs : 0.0;
        double loss = sself->_lastLossPct;
        [sself sendJson:@{
            @"t" : @"stats",
            @"fps" : @(round(fpsNow * 10.0) / 10.0),
            @"kbps" : @(round(kbps)),
            @"enc_ms" : @(round(encMs * 10.0) / 10.0),
            @"loss_pct" : @(round(loss * 10.0) / 10.0),
            @"nacks" : @(dResent),
            @"sent" : @(dSent),
        }];
        id<OCNetManagerDelegate> d = sself->_delegate;
        if (d && [d respondsToSelector:@selector(netManager:didUpdateStatsFps:kbps:encMs:lossPct:nacks:sent:)]) {
            dispatch_async(dispatch_get_main_queue(), ^{
                [d netManager:sself didUpdateStatsFps:fpsNow kbps:kbps
                        encMs:encMs lossPct:loss nacks:dResent sent:dSent];
            });
        }
    });
    _statsTimer = t;
    dispatch_resume(t);
}

#pragma mark JSON helpers

- (double)numIn:(NSDictionary *)dict key:(NSString *)key def:(double)def {
    id v = dict[key];
    return [v isKindOfClass:[NSNumber class]] ? [v doubleValue] : def;
}

- (BOOL)flagIn:(NSDictionary *)dict key:(NSString *)key def:(BOOL)def {
    id v = dict[key];
    return [v isKindOfClass:[NSNumber class]] ? [v boolValue] : def;
}

- (double)num:(NSDictionary *)dict key:(NSString *)key def:(double)def {
    return [self numIn:dict key:key def:def];
}

@end
