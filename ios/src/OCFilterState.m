//
//  OCFilterState.m — PROTOCOL.md §5 model: defaults, (de)serialization, persistence.
//
#import "OCFilterState.h"

NSNotificationName const OCFilterStateDidChangeNotification = @"OCFilterStateDidChangeNotification";
NSString * const OCFilterStateDefaultsKey = @"omnicam.filter.v1";

static CGFloat ocClamp(CGFloat v, CGFloat lo, CGFloat hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

static NSDictionary *ocDict(NSDictionary *d, NSString *key) {
    id v = d[key];
    return [v isKindOfClass:[NSDictionary class]] ? v : @{};
}

static double ocNum(NSDictionary *d, NSString *key, double def) {
    id v = d[key];
    if ([v isKindOfClass:[NSNumber class]]) return [v doubleValue];
    if ([v isKindOfClass:[NSString class]]) return [v doubleValue];
    return def;
}

static BOOL ocFlag(NSDictionary *d, NSString *key, BOOL def) {
    id v = d[key];
    if ([v isKindOfClass:[NSNumber class]]) return [v boolValue];
    if ([v isKindOfClass:[NSString class]]) return [v boolValue];
    return def;
}

static NSString *ocStr(NSDictionary *d, NSString *key, NSString *def) {
    id v = d[key];
    if ([v isKindOfClass:[NSString class]]) return v;
    return def;
}

static NSString *ocOneOf(NSDictionary *d, NSString *key, NSString *def, NSArray<NSString *> *valid) {
    NSString *s = ocStr(d, key, def);
    return [valid containsObject:s] ? s : def;
}

@implementation OCFilterState

+ (NSArray<NSString *> *)validLooks {
    // Order matters only for the UI grid; strings are protocol-identifiers (§5).
    return @[@"none", @"mono", @"noir", @"chrome", @"fade", @"instant", @"process",
             @"transfer", @"sepia", @"invert", @"false_color"];
}

+ (NSArray<NSString *> *)validStylizes {
    return @[@"none", @"pixelate", @"crystallize", @"hexagonal", @"twirl",
             @"bulge", @"bump", @"soft_blur", @"zoom_blur"];
}

+ (NSArray<NSString *> *)validAspects {
    return @[@"native", @"16:9", @"4:3"];
}

- (instancetype)init {
    self = [super init];
    if (self) {
        [self resetToDefaultsLocked];
    }
    return self;
}

- (void)resetToDefaultsLocked {
    _look = @"none";
    _brightness = 0.0;
    _contrast = 0.0;
    _saturation = 1.0;
    _temperature = 0.0;
    _vibrance = 0.0;
    _gamma = 1.0;
    _sharpness = 0.0;
    _vignette = 0.0;
    _beauty = 0.0;
    _stylize = @"none";
    _stylizeAmount = 0.5;
    _mirror = NO;
    _flipV = NO;
    _rotate = 0;
    _zoom = 1.0;
    _panX = 0.0;
    _panY = 0.0;
    _aspect = @"native";
    _lut = nil;
    _overlayText = @"";
    _showTimecode = NO;
}

+ (instancetype)restoredState {
    OCFilterState *s = [[OCFilterState alloc] init];
    NSDictionary *saved = [[NSUserDefaults standardUserDefaults] dictionaryForKey:OCFilterStateDefaultsKey];
    if (saved.count > 0) {
        // loadFromDictionary tolerates missing keys and persists + posts.
        [s loadFromDictionary:saved];
    }
    return s;
}

- (void)reset {
    [self resetToDefaultsLocked];
    [self saveToDefaults];
    [[NSNotificationCenter defaultCenter] postNotificationName:OCFilterStateDidChangeNotification object:self];
}

- (void)performUpdate:(void (NS_NOESCAPE ^)(OCFilterState *state))block {
    if (block) block(self);
    [self clampValues];
    [self saveToDefaults];
    [[NSNotificationCenter defaultCenter] postNotificationName:OCFilterStateDidChangeNotification object:self];
}

#pragma mark Serialization (§5)

- (NSDictionary *)dictionaryRepresentation {
    return @{
        @"look": _look ?: @"none",
        @"adjust": @{
            @"brightness": @(_brightness),
            @"contrast": @(_contrast),
            @"saturation": @(_saturation),
            @"temperature": @(_temperature),
            @"vibrance": @(_vibrance),
            @"gamma": @(_gamma),
            @"sharpness": @(_sharpness),
            @"vignette": @(_vignette),
        },
        @"beauty": @(_beauty),
        @"stylize": _stylize ?: @"none",
        @"stylize_amount": @(_stylizeAmount),
        @"geometry": @{
            @"mirror": @(_mirror),
            @"flipV": @(_flipV),
            @"rotate": @(_rotate),
            @"zoom": @(_zoom),
            @"panX": @(_panX),
            @"panY": @(_panY),
            @"aspect": _aspect ?: @"native",
        },
        @"lut": _lut ?: (id)NSNull.null,
        @"overlay": @{
            @"text": _overlayText ?: @"",
            @"show_timecode": @(_showTimecode),
        },
    };
}

- (void)loadFromDictionary:(NSDictionary *)dict {
    if (![dict isKindOfClass:[NSDictionary class]]) return;

    NSDictionary *adj = ocDict(dict, @"adjust");
    NSDictionary *geo = ocDict(dict, @"geometry");
    NSDictionary *ovl = ocDict(dict, @"overlay");

    _look          = ocOneOf(dict, @"look", @"none", [OCFilterState validLooks]);
    _brightness    = ocClamp(ocNum(adj, @"brightness", 0.0), -1.0, 1.0);
    _contrast      = ocClamp(ocNum(adj, @"contrast", 0.0), -1.0, 1.0);
    _saturation    = ocClamp(ocNum(adj, @"saturation", 1.0), 0.0, 2.0);
    _temperature   = ocClamp(ocNum(adj, @"temperature", 0.0), -1.0, 1.0);
    _vibrance      = ocClamp(ocNum(adj, @"vibrance", 0.0), 0.0, 1.0);
    _gamma         = ocClamp(ocNum(adj, @"gamma", 1.0), 0.2, 3.0);
    _sharpness     = ocClamp(ocNum(adj, @"sharpness", 0.0), 0.0, 1.0);
    _vignette      = ocClamp(ocNum(adj, @"vignette", 0.0), 0.0, 1.0);
    _beauty        = ocClamp(ocNum(dict, @"beauty", 0.0), 0.0, 1.0);
    _stylize       = ocOneOf(dict, @"stylize", @"none", [OCFilterState validStylizes]);
    _stylizeAmount = ocClamp(ocNum(dict, @"stylize_amount", 0.5), 0.0, 1.0);
    _mirror        = ocFlag(geo, @"mirror", NO);
    _flipV         = ocFlag(geo, @"flipV", NO);
    _rotate        = (NSInteger)ocNum(geo, @"rotate", 0);
    _zoom          = ocClamp(ocNum(geo, @"zoom", 1.0), 0.5, 8.0);
    _panX          = ocClamp(ocNum(geo, @"panX", 0.0), -1.0, 1.0);
    _panY          = ocClamp(ocNum(geo, @"panY", 0.0), -1.0, 1.0);
    _aspect        = ocOneOf(geo, @"aspect", @"native", [OCFilterState validAspects]);

    id lut = dict[@"lut"];
    _lut = ([lut isKindOfClass:[NSString class]] && [(NSString *)lut length] > 0) ? lut : nil;

    _overlayText   = ocStr(ovl, @"text", @"");
    _showTimecode  = ocFlag(ovl, @"show_timecode", NO);

    [self clampValues];
    [self saveToDefaults];
    [[NSNotificationCenter defaultCenter] postNotificationName:OCFilterStateDidChangeNotification object:self];
}

// NSUserDefaults only accepts property-list objects. The protocol dictionary
// carries "lut": null (JSON null) when no LUT is selected — writing that NSNull
// raised NSInvalidArgumentException on every slider change and killed the app.
static id ocPlistSafe(id obj) {
    if (obj == nil || obj == NSNull.null) return nil;
    if ([obj isKindOfClass:[NSDictionary class]]) {
        NSMutableDictionary *out = [NSMutableDictionary dictionaryWithCapacity:[obj count]];
        [(NSDictionary *)obj enumerateKeysAndObjectsUsingBlock:^(id key, id val, BOOL *stop) {
            id safe = ocPlistSafe(val);
            if (safe && [key isKindOfClass:[NSString class]]) out[key] = safe;
        }];
        return out;
    }
    if ([obj isKindOfClass:[NSArray class]]) {
        NSMutableArray *out = [NSMutableArray arrayWithCapacity:[obj count]];
        for (id v in (NSArray *)obj) {
            id safe = ocPlistSafe(v);
            if (safe) [out addObject:safe];
        }
        return out;
    }
    if ([obj isKindOfClass:[NSString class]] || [obj isKindOfClass:[NSNumber class]] ||
        [obj isKindOfClass:[NSData class]] || [obj isKindOfClass:[NSDate class]]) {
        return obj;
    }
    return [obj description];
}

- (void)saveToDefaults {
    id plist = ocPlistSafe([self dictionaryRepresentation]);
    if (!plist) return;
    @try {
        [[NSUserDefaults standardUserDefaults] setObject:plist forKey:OCFilterStateDefaultsKey];
    } @catch (NSException *ex) {
        NSLog(@"[OmniCam] filter state save failed: %@", ex);
    }
}

#pragma mark Validation

- (void)clampValues {
    // Normalize enum-ish strings to protocol identifiers (§5).
    if (![[_look class] isSubclassOfClass:[NSString class]] ||
        ![[OCFilterState validLooks] containsObject:_look]) {
        _look = @"none";
    }
    if (![[_stylize class] isSubclassOfClass:[NSString class]] ||
        ![[OCFilterState validStylizes] containsObject:_stylize]) {
        _stylize = @"none";
    }
    if (![[_aspect class] isSubclassOfClass:[NSString class]] ||
        ![[OCFilterState validAspects] containsObject:_aspect]) {
        _aspect = @"native";
    }
    _brightness = ocClamp(_brightness, -1.0, 1.0);
    _contrast   = ocClamp(_contrast, -1.0, 1.0);
    _saturation = ocClamp(_saturation, 0.0, 2.0);
    _temperature = ocClamp(_temperature, -1.0, 1.0);
    _vibrance   = ocClamp(_vibrance, 0.0, 1.0);
    _gamma      = ocClamp(_gamma, 0.2, 3.0);
    _sharpness  = ocClamp(_sharpness, 0.0, 1.0);
    _vignette   = ocClamp(_vignette, 0.0, 1.0);
    _beauty     = ocClamp(_beauty, 0.0, 1.0);
    _stylizeAmount = ocClamp(_stylizeAmount, 0.0, 1.0);
    _zoom       = ocClamp(_zoom, 0.5, 8.0);
    _panX       = ocClamp(_panX, -1.0, 1.0);
    _panY       = ocClamp(_panY, -1.0, 1.0);

    NSInteger r = ((NSInteger)_rotate % 360 + 360) % 360;
    _rotate = (NSInteger)(lround(r / 90.0) * 90) % 360;

    if (_overlayText.length == 0) {
        _overlayText = @"";
    }
    if (_lut.length == 0) {
        _lut = nil;
    }
}

#pragma mark Identity

- (BOOL)isIdentity {
    if (![_look isEqualToString:@"none"]) return NO;
    if (self.brightness != 0.0 || self.contrast != 0.0 || self.saturation != 1.0 ||
        self.temperature != 0.0 || self.vibrance != 0.0 || self.gamma != 1.0 ||
        self.sharpness != 0.0 || self.vignette != 0.0) return NO;
    if (self.beauty != 0.0) return NO;
    if (![_stylize isEqualToString:@"none"]) return NO;
    if (self.mirror || self.flipV || self.rotate != 0) return NO;
    if (self.zoom < 0.9999 || self.zoom > 1.0001) return NO;
    if (fabs(self.panX) > 0.0001 || fabs(self.panY) > 0.0001) return NO;
    if (![_aspect isEqualToString:@"native"]) return NO;
    if (self.lut != nil) return NO;
    if (self.overlayText.length > 0 || self.showTimecode) return NO;
    return YES;
}

@end
