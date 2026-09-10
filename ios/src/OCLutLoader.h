//
//  OCLutLoader.h — .cube 3D LUT parser producing CIColorCube-ready data.
//
//  Output format: premultiplied RGBA float32, blue-fastest ordering:
//      index = (r*size^2 + g*size + b) * 4
//  Sizes are clamped to <= 64.
#import <Foundation/Foundation.h>
#import <CoreImage/CoreImage.h>

NS_ASSUME_NONNULL_BEGIN

/// Posted on the main thread whenever importCubeFileAtURL: succeeds.
FOUNDATION_EXPORT NSNotificationName const OCLutsDidChangeNotification;

@interface OCLutLoader : NSObject

- (instancetype)init NS_DESIGNATED_INITIALIZER;

/// Returns cached cube data (float32 RGBA, blue-fastest) for <Documents>/<name>.cube,
/// or nil if the file is missing/invalid.
- (nullable NSData *)cubeDataForLutNamed:(NSString *)name;

/// Convenience: applies the LUT (CIColorCube) to the image. name nil/unknown → returns image unchanged.
- (CIImage *)applyLutNamed:(nullable NSString *)name toImage:(CIImage *)image;

- (void)clearCache;

+ (NSString *)lutsDirectory;
+ (NSArray<NSString *> *)availableLutNames;
/// Copies the file into Documents (overwriting same-named files) and posts OCLutsDidChangeNotification.
+ (BOOL)importCubeFileAtURL:(NSURL *)url error:(NSError **)error;

@end

NS_ASSUME_NONNULL_END
