//
//  OCViewController.h — programmatic UIKit UI + engine wiring (no storyboards).
//
//  Owns the whole object graph:
//    OCCaptureEngine → OCFilterPipeline → OCEncoder → OCPacker → OCNetManager
//  Frame path: capture → (filter or zero-copy) → encode → pack → UDP.
#import <UIKit/UIKit.h>
#import "OCFilterState.h"

NS_ASSUME_NONNULL_BEGIN

@interface OCViewController : UIViewController

/// Shared mutable filter state (persisted); also referenced weakly by OCNetManager.
@property (nonatomic, strong, readonly) OCFilterState *filterState;

/// Designated initializer (calls super initWithNibName:nil bundle:nil internally).
- (instancetype)initWithFilterState:(OCFilterState *)state;

@end

NS_ASSUME_NONNULL_END
