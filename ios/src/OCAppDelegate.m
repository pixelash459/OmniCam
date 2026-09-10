//
//  OCAppDelegate.m — window setup + "Open in OmniCam" .cube import (Filza)
//
#import "OCAppDelegate.h"
#import "OCViewController.h"
#import "OCFilterState.h"
#import "OCLutLoader.h"

#include <stdio.h>
#include <unistd.h>

NSNotificationName const OCImportedLutsDidChangeNotification = @"OCImportedLutsDidChangeNotification";

// Mirror NSLog (stderr) into Documents/omnicam.log so a glitch can be diagnosed
// over SSH after the fact (there is no `log`/`oslog` tool on the jailbroken
// phone). Rotates once the file passes 2 MiB. Skipped when a debugger has a tty.
static void OCRedirectLogToFile(void) {
    if (isatty(STDERR_FILENO)) return;
    NSString *dir = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES).firstObject;
    if (!dir) return;
    NSString *path = [dir stringByAppendingPathComponent:@"omnicam.log"];
    NSString *prev = [dir stringByAppendingPathComponent:@"omnicam.prev.log"];
    NSFileManager *fm = [NSFileManager defaultManager];
    NSDictionary *attrs = [fm attributesOfItemAtPath:path error:nil];
    if (attrs && [attrs fileSize] > 2u * 1024u * 1024u) {
        [fm removeItemAtPath:prev error:nil];
        [fm moveItemAtPath:path toPath:prev error:nil];
    }
    if (!freopen(path.fileSystemRepresentation, "a+", stderr)) return;
    setvbuf(stderr, NULL, _IOLBF, 0);
    NSLog(@"[OmniCam] ---- launch, logging to %@ ----", path);
}

@implementation OCAppDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    OCRedirectLogToFile();
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
