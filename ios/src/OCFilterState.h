//
//  OCFilterState.h — filter state model, EXACTLY matching PROTOCOL.md §5.
//
//  JSON schema (defaults marked *):
//  {
//    "look": "none"*|mono|noir|chrome|fade|instant|process|transfer|sepia|invert|false_color,
//    "adjust": {"brightness":0*, "contrast":0*, "saturation":1*, "temperature":0*,
//               "vibrance":0*, "gamma":1*, "sharpness":0*, "vignette":0*},
//    "beauty":0*,
//    "stylize":"none"*|pixelate|crystallize|hexagonal|twirl|bulge|bump|soft_blur|zoom_blur,
//    "stylize_amount":0.5*,
//    "geometry":{"mirror":false*,"flipV":false*,"rotate":0*,"zoom":1*,"panX":0*,"panY":0*,"aspect":"native"*},
//    "lut":null*,
//    "overlay":{"text":"","show_timecode":false*}
//  }
//
//  Concurrency contract: mutations happen on the main thread only (UI and PC-pushed
//  state are both dispatched there). The render thread reads properties without a
//  lock; worst case is one frame rendered with partially-updated scalars, which is
//  visually harmless.
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Posted synchronously (on the mutating thread — main by convention) after any committed change.
FOUNDATION_EXPORT NSNotificationName const OCFilterStateDidChangeNotification;

extern NSString * const OCFilterStateDefaultsKey;

@interface OCFilterState : NSObject

@property (nonatomic, copy) NSString *look;          // "none"...
@property (nonatomic, assign) CGFloat brightness;    // -1..1
@property (nonatomic, assign) CGFloat contrast;      // -1..1
@property (nonatomic, assign) CGFloat saturation;    // 0..2
@property (nonatomic, assign) CGFloat temperature;   // -1..1 (cool..warm)
@property (nonatomic, assign) CGFloat vibrance;      // 0..1
@property (nonatomic, assign) CGFloat gamma;         // 0.2..3
@property (nonatomic, assign) CGFloat sharpness;     // 0..1
@property (nonatomic, assign) CGFloat vignette;      // 0..1
@property (nonatomic, assign) CGFloat beauty;        // 0..1
@property (nonatomic, copy) NSString *stylize;       // "none"...
@property (nonatomic, assign) CGFloat stylizeAmount; // 0..1
@property (nonatomic, assign) BOOL mirror;
@property (nonatomic, assign) BOOL flipV;
@property (nonatomic, assign) NSInteger rotate;      // 0|90|180|270
@property (nonatomic, assign) CGFloat zoom;          // >=1 typically
@property (nonatomic, assign) CGFloat panX;          // -1..1 (fraction of width)
@property (nonatomic, assign) CGFloat panY;          // -1..1 (fraction of height)
@property (nonatomic, copy) NSString *aspect;        // "native"|"16:9"|"4:3"
@property (nonatomic, copy, nullable) NSString *lut; // .cube filename in Documents, or nil
@property (nonatomic, copy) NSString *overlayText;
@property (nonatomic, assign) BOOL showTimecode;

+ (NSArray<NSString *> *)validLooks;
+ (NSArray<NSString *> *)validStylizes;
+ (NSArray<NSString *> *)validAspects;

/// Restores the persisted state from NSUserDefaults (falls back to defaults).
+ (instancetype)restoredState;

/// Resets every field to the schema defaults (posts change + persists).
- (void)reset;

/// Applies a mutation, clamps/validates, persists and posts OCFilterStateDidChangeNotification.
- (void)performUpdate:(void (NS_NOESCAPE ^)(OCFilterState *state))block;

/// Tolerant deserialization: missing/invalid keys fall back to defaults. Persists + posts.
- (void)loadFromDictionary:(NSDictionary *)dict;

/// Serialization exactly matching the §5 schema (lut is NSNull when unset).
- (NSDictionary *)dictionaryRepresentation;

- (void)saveToDefaults;

/// YES when equal to schema defaults in every field (lut nil, overlay empty). The
/// filter pipeline uses this to take the zero-copy pass-through path.
- (BOOL)isIdentity;

@end

NS_ASSUME_NONNULL_END
