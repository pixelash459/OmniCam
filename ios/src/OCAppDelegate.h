//
//  OCAppDelegate.h — OmniCam application delegate
//
#import <UIKit/UIKit.h>

NS_ASSUME_NONNULL_BEGIN

/// Posted on the main thread after a .cube file was imported into Documents.
FOUNDATION_EXPORT NSNotificationName const OCImportedLutsDidChangeNotification;

@interface OCAppDelegate : UIResponder <UIApplicationDelegate>

@property (nonatomic, strong, nullable) UIWindow *window;

@end

NS_ASSUME_NONNULL_END
