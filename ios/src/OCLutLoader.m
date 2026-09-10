//
//  OCLutLoader.m — standard .cube text parser → premultiplied RGBA float32 cube data.
//
#import "OCLutLoader.h"

#import <stdlib.h>
#include <math.h>

NSNotificationName const OCLutsDidChangeNotification = @"OCLutsDidChangeNotification";

static const NSUInteger OCLutMaxSize = 64;

@interface OCLutLoader ()
@property (nonatomic, strong) NSCache<NSString *, NSData *> *cache;
@end

@implementation OCLutLoader

- (instancetype)init {
    self = [super init];
    if (self) {
        _cache = [[NSCache alloc] init];
        _cache.countLimit = 8;
    }
    return self;
}

+ (NSString *)lutsDirectory {
    NSString *docs = [NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES) firstObject];
    return docs ?: NSTemporaryDirectory();
}

+ (NSArray<NSString *> *)availableLutNames {
    NSArray<NSURL *> *items = [[NSFileManager defaultManager]
        contentsOfDirectoryAtURL:[NSURL fileURLWithPath:[self lutsDirectory]]
      includingPropertiesForKeys:nil options:0 error:nil];
    NSMutableArray<NSString *> *names = [NSMutableArray array];
    for (NSURL *u in items) {
        if ([u.pathExtension.lowercaseString isEqualToString:@"cube"]) {
            [names addObject:u.lastPathComponent];
        }
    }
    return [names sortedArrayUsingSelector:@selector(caseInsensitiveCompare:)];
}

+ (BOOL)importCubeFileAtURL:(NSURL *)url error:(NSError **)error {
    if (!url || ![[url pathExtension].lowercaseString isEqualToString:@"cube"]) {
        if (error) {
            *error = [NSError errorWithDomain:@"OCLutLoader" code:1
                          userInfo:@{NSLocalizedDescriptionKey: @"Not a .cube file"}];
        }
        return NO;
    }
    NSString *dest = [[self lutsDirectory] stringByAppendingPathComponent:url.lastPathComponent];
    NSFileManager *fm = [NSFileManager defaultManager];
    if ([fm fileExistsAtPath:dest]) {
        [fm removeItemAtPath:dest error:nil]; // overwrite semantics
    }
    // Same-name upload from the app delegate must survive the caller's temp dir cleanup.
    BOOL ok = [fm copyItemAtURL:url toURL:[NSURL fileURLWithPath:dest] error:error];
    if (ok) {
        dispatch_async(dispatch_get_main_queue(), ^{
            [[NSNotificationCenter defaultCenter] postNotificationName:OCLutsDidChangeNotification object:nil];
        });
    }
    return ok;
}

#pragma mark Parsing

// Parses .cube content. On success returns premultiplied RGBA float32 data laid out
// blue-fastest: (r*size^2 + g*size + b) * 4. Returns nil for 1D-only or invalid files.
- (nullable NSData *)cubeDataFromString:(NSString *)content size:(NSUInteger *)outSize {
    if (content.length == 0) return nil;

    NSUInteger size = 0;
    double domainMin[3] = {0.0, 0.0, 0.0};
    double domainMax[3] = {1.0, 1.0, 1.0};
    NSMutableArray<NSNumber *> *entries = [NSMutableArray array]; // file order: red fastest

    NSCharacterSet *ws = [NSCharacterSet whitespaceAndNewlineCharacterSet];
    for (NSString *rawLine in [content componentsSeparatedByCharactersInSet:[NSCharacterSet newlineCharacterSet]]) {
        NSString *line = [rawLine stringByTrimmingCharactersInSet:ws];
        if (line.length == 0 || [line hasPrefix:@"#"]) continue;

        NSArray<NSString *> *tok = [line componentsSeparatedByCharactersInSet:ws];
        NSString *kw = tok.firstObject.uppercaseString;

        if ([kw isEqualToString:@"TITLE"]) {
            continue; // informational only
        } else if ([kw isEqualToString:@"LUT_3D_SIZE"] && tok.count >= 2) {
            size = (NSUInteger)tok[1].integerValue;
        } else if ([kw isEqualToString:@"LUT_1D_SIZE"]) {
            // 1D LUTs are not usable for CIColorCube color grading here; keep scanning,
            // a combined file may still declare LUT_3D_SIZE.
            continue;
        } else if ([kw isEqualToString:@"DOMAIN_MIN"] && tok.count >= 4) {
            domainMin[0] = tok[1].doubleValue; domainMin[1] = tok[2].doubleValue; domainMin[2] = tok[3].doubleValue;
        } else if ([kw isEqualToString:@"DOMAIN_MAX"] && tok.count >= 4) {
            domainMax[0] = tok[1].doubleValue; domainMax[1] = tok[2].doubleValue; domainMax[2] = tok[3].doubleValue;
        } else if (tok.count >= 3 && kw.length > 0) {
            unichar c0 = [kw characterAtIndex:0];
            BOOL numeric = (c0 >= '0' && c0 <= '9') || c0 == '-' || c0 == '+' || c0 == '.';
            if (numeric) {
                [entries addObject:@(tok[0].doubleValue)];
                [entries addObject:@(tok[1].doubleValue)];
                [entries addObject:@(tok[2].doubleValue)];
            }
        }
    }

    if (size == 0 || size > OCLutMaxSize) return nil;
    NSUInteger cube = size * size * size;
    if (entries.count < cube * 3) return nil;

    NSMutableData *data = [NSMutableData dataWithLength:cube * 4 * sizeof(float)];
    float *out = (float *)data.mutableBytes;
    double dr = domainMax[0] - domainMin[0];
    double dg = domainMax[1] - domainMin[1];
    double db = domainMax[2] - domainMin[2];
    if (dr <= 0) dr = 1; if (dg <= 0) dg = 1; if (db <= 0) db = 1;

    float *vals = (float *)malloc(cube * 3 * sizeof(float));
    if (!vals) return nil;
    for (NSUInteger i = 0; i < cube * 3; i++) {
        vals[i] = (float)[entries[i] doubleValue];
    }
    for (NSUInteger i = 0; i < cube; i++) {
        // .cube files are red-fastest; remap to blue-fastest for CIColorCube.
        NSUInteger r = i % size;
        NSUInteger g = (i / size) % size;
        NSUInteger b = i / (size * size);
        float rr = (vals[i * 3 + 0] - (float)domainMin[0]) / (float)dr;
        float gg = (vals[i * 3 + 1] - (float)domainMin[1]) / (float)dg;
        float bb = (vals[i * 3 + 2] - (float)domainMin[2]) / (float)db;
        NSUInteger o = (r * size * size + g * size + b) * 4;
        out[o + 0] = fmaxf(0.0f, fminf(1.0f, rr)); // alpha 1 → premultiplied == unpremultiplied
        out[o + 1] = fmaxf(0.0f, fminf(1.0f, gg));
        out[o + 2] = fmaxf(0.0f, fminf(1.0f, bb));
        out[o + 3] = 1.0f;
    }
    free(vals);
    if (outSize) *outSize = size;
    return data;
}

- (nullable NSData *)cubeDataForLutNamed:(NSString *)name {
    if (name.length == 0) return nil;
    NSData *cached = [_cache objectForKey:name];
    if (cached) return cached;

    NSString *path = [[OCLutLoader lutsDirectory] stringByAppendingPathComponent:name];
    NSError *err = nil;
    NSString *content = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:&err];
    if (!content) {
        content = [NSString stringWithContentsOfFile:path
                                            encoding:[NSString defaultCStringEncoding]
                                               error:nil];
    }
    if (!content) return nil;

    NSData *cube = [self cubeDataFromString:content size:NULL];
    if (cube) [_cache setObject:cube forKey:name];
    return cube;
}

- (CIImage *)applyLutNamed:(NSString *)name toImage:(CIImage *)image {
    if (name.length == 0 || !image) return image;
    NSData *cube = [self cubeDataForLutNamed:name];
    if (!cube) return image;
    CIFilter *f = [CIFilter filterWithName:@"CIColorCube"];
    if (!f) return image;
    [f setValue:image forKey:kCIInputImageKey];
    // CIColorCube infers n from cubeData length (must be n^3 * 4 floats, premultiplied).
    [f setValue:cube forKey:@"inputCubeData"];
    return f.outputImage ?: image;
}

- (void)clearCache {
    [_cache removeAllObjects];
}

@end
