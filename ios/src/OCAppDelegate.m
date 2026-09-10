//
//  OCAppDelegate.m — window setup + "Open in OmniCam" .cube import (Filza)
//
#import "OCAppDelegate.h"
#import "OCViewController.h"
#import "OCFilterState.h"
#import "OCLutLoader.h"

NSNotificationName const OCImportedLutsDidChangeNotification = @"OCImportedLutsDidChangeNotification";

@implementation OCAppDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    self.window = [[UIWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
    self.window.backgroundColor = [UIColor blackColor];
    OCViewController *vc = [[OCViewController alloc] initWithFilterState:[OCFilterState restoredState]];
    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];
    return YES;
}

// Handles "Open in OmniCam" from Filza for .cube LUT files: copies them into
// NSDocumentDirectory where OCLutLoader + the filter panel find them.
- (BOOL)application:(UIApplication *)app openURL:(NSURL *)url options:(NSDictionary<UIApplicationOpenURLOptionsKey, id> *)options {
    if (!url) return NO;
    NSString *ext = url.pathExtension.lowercaseString;
    if (![ext isEqualToString:@"cube"]) return NO;
    NSError *err = nil;
    if (![OCLutLoader importCubeFileAtURL:url error:&err]) {
        NSLog(@"[OmniCam] LUT import failed: %@", err);
        return NO;
    }
    dispatch_async(dispatch_get_main_queue(), ^{
        [[NSNotificationCenter defaultCenter] postNotificationName:OCImportedLutsDidChangeNotification object:nil];
    });
    return YES;
}

- (void)applicationWillResignActive:(UIApplication *)application {}

// No background modes: capture dies when backgrounded; VC stops the stream itself.
- (void)applicationDidEnterBackground:(UIApplication *)application {}

- (void)applicationDidBecomeActive:(UIApplication *)application {}

@end
