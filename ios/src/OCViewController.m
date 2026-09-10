//
//  OCViewController.m — full-screen camera UI: preview layer / MTKView WYSIWYG,
//  stream controls, filter panel, stats HUD.
//
#import "OCViewController.h"

#import <MetalKit/MetalKit.h>       // NOTE: app target needs the MetalKit framework
#import <AVFoundation/AVFoundation.h>
#import "OCCaptureEngine.h"
#import "OCFilterPipeline.h"
#import "OCEncoder.h"
#import "OCPacker.h"
#import "OCNetManager.h"
#import "OCLutLoader.h"
#import "OCAppDelegate.h"

// Slider tags — one handler dispatches on tag.
typedef NS_ENUM(NSInteger, OCSliderTag) {
    OCSliderBrightness = 200, OCSliderContrast, OCSliderSaturation, OCSliderTemperature,
    OCSliderVibrance, OCSliderGamma, OCSliderSharpness, OCSliderVignette,
    OCSliderBeauty, OCSliderStylizeAmount, OCSliderGeoZoom, OCSliderPanX, OCSliderPanY,
    OCSliderCameraZoom = 299,
};

static UIColor *OCAccent(void) {
    static UIColor *c;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ c = [UIColor colorWithRed:1.0 green:0.48 blue:0.10 alpha:1.0]; });
    return c;
}

@interface OCViewController () <OCNetManagerDelegate,
                                OCFilterPipelinePreviewDelegate,
                                MTKViewDelegate,
                                UITextFieldDelegate,
                                UIDocumentPickerDelegate>
// Engine graph (strong — the VC owns everything).
@property (nonatomic, strong, readwrite) OCFilterState *filterState;
@property (nonatomic, strong) OCCaptureEngine *captureEngine;
@property (nonatomic, strong) OCFilterPipeline *pipeline;
@property (nonatomic, strong) OCEncoder *encoder;
@property (nonatomic, strong) OCPacker *packer;
@property (nonatomic, strong) OCNetManager *netManager;

// Preview
@property (nonatomic, strong) AVCaptureVideoPreviewLayer *previewLayer;
@property (nonatomic, strong, nullable) MTKView *mtkView;
@property (nonatomic, assign, nullable) CGColorSpaceRef rgbSpace;

// Top HUD
@property (nonatomic, strong) UILabel *statsLabel;
@property (nonatomic, strong) UILabel *statusLabel;
@property (nonatomic, strong) UILabel *ipLabel;

// Bottom controls
@property (nonatomic, strong) UIButton *startButton;
@property (nonatomic, strong) UIButton *cameraButton;
@property (nonatomic, strong) UIButton *torchButton;
@property (nonatomic, strong) UISegmentedControl *resControl;
@property (nonatomic, strong) UILabel *bitrateLabel;
@property (nonatomic, strong) UISwitch *abrSwitch;
@property (nonatomic, strong) UILabel *zoomValueLabel;
@property (nonatomic, strong) UISlider *cameraZoomSlider;
@property (nonatomic, strong) UIButton *panelToggleButton;

// Filter panel
@property (nonatomic, strong) UIView *panel;
@property (nonatomic, strong) UIScrollView *panelScroll;
@property (nonatomic, strong) NSMutableDictionary<NSNumber *, UISlider *> *sliderByTag;
@property (nonatomic, strong) NSMutableDictionary<NSNumber *, UILabel *> *valueByTag;
@property (nonatomic, strong) NSMutableArray<UIButton *> *lookButtons;
@property (nonatomic, strong) NSMutableArray<UIButton *> *stylizeButtons;
@property (nonatomic, strong) UISwitch *mirrorSwitch;
@property (nonatomic, strong) UISwitch *flipSwitch;
@property (nonatomic, strong) UISegmentedControl *rotateControl;
@property (nonatomic, strong) UISegmentedControl *aspectControl;
@property (nonatomic, strong) UITextField *overlayField;
@property (nonatomic, strong) UISwitch *timecodeSwitch;
@property (nonatomic, strong) UIView *lutList;
@property (nonatomic, assign) BOOL panelOpen;
@end

@implementation OCViewController

- (instancetype)initWithFilterState:(OCFilterState *)state {
    NSAssert(state != nil, @"OCViewController requires a filter state");
    self = [super initWithNibName:nil bundle:nil];
    if (self) {
        _filterState = state;
        _sliderByTag = [NSMutableDictionary dictionary];
        _valueByTag = [NSMutableDictionary dictionary];
        _lookButtons = [NSMutableArray array];
        _stylizeButtons = [NSMutableArray array];
    }
    return self;
}

#pragma mark Lifecycle

- (void)loadView {
    self.view = [[UIView alloc] initWithFrame:[UIScreen mainScreen].bounds];
    self.view.backgroundColor = [UIColor blackColor];

    _previewLayer = [AVCaptureVideoPreviewLayer layer];
    _previewLayer.videoGravity = AVLayerVideoGravityResizeAspectFill;
    _previewLayer.backgroundColor = [UIColor blackColor].CGColor;
    _previewLayer.frame = self.view.bounds;
    [self.view.layer insertSublayer:_previewLayer atIndex:0];
}

- (void)viewDidLoad {
    [super viewDidLoad];
    [self buildEngines];
    [self buildUI];
    [self applyStateToUI];
    [self updatePreviewMode];
    [self observeLife];
    [self requestPermissionsAndStart];
    [_netManager start];
}

- (void)dealloc {
    [[NSNotificationCenter defaultCenter] removeObserver:self];
    if (_rgbSpace) CGColorSpaceRelease(_rgbSpace);
}

#pragma mark Engine wiring

- (void)buildEngines {
    _captureEngine = [[OCCaptureEngine alloc] init];

    _encoder = [[OCEncoder alloc] init];

    _pipeline = [[OCFilterPipeline alloc] initWithFilterState:_filterState];
    _pipeline.encoder = _encoder;         // render into the encoder's buffer pool
    _pipeline.previewDelegate = self;

    _captureEngine.delegate = _pipeline;  // capture → filter → encode → pack → UDP
    _pipeline.delegate = _encoder;

    _packer = [[OCPacker alloc] init];
    _encoder.delegate = _packer;

    _netManager = [[OCNetManager alloc] init];
    _netManager.delegate = self;
    _netManager.captureEngine = _captureEngine;
    _netManager.encoder = _encoder;
    _netManager.packer = _packer;
    _netManager.filterState = _filterState;
    // Without these, Annex-B is produced but encoder:didProduceAnnexB: bails
    // on a nil packer.network and never UDP-sends RTP (TCP control still works).
    _packer.network = _netManager;
    _packer.encoder = _encoder;

    _previewLayer.session = _captureEngine.session;
}

- (void)requestPermissionsAndStart {
    [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL camGranted) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (!camGranted) {
                [self showAlert:@"Camera access denied"
                        message:@"OmniCam needs the camera. Enable it in Settings → Privacy → Camera."];
                _statusLabel.text = @"camera denied";
                return;
            }
            dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
                NSError *err = nil;
                if (![_captureEngine startAndReturnError:&err]) {
                    dispatch_async(dispatch_get_main_queue(), ^{
                        _statusLabel.text = @"camera error";
                        [self showAlert:@"Camera error" message:err.localizedDescription ?: @"Capture failed to start"];
                    });
                } else {
                    dispatch_async(dispatch_get_main_queue(), ^{
                        [self refreshCameraControls];
                    });
                }
            });
        });
    }];
}

- (void)observeLife {
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(filterChanged:)
                                                 name:OCFilterStateDidChangeNotification
                                               object:_filterState];
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(lutsChanged:)
                                                 name:OCImportedLutsDidChangeNotification
                                               object:nil];
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(appBackgrounded:)
                                                 name:UIApplicationDidEnterBackgroundNotification
                                               object:nil];
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(appActive:)
                                                 name:UIApplicationDidBecomeActiveNotification
                                               object:nil];
}

- (void)appBackgrounded:(NSNotification *)n {
    // Capture cannot run in background (no bg modes): drop the stream cleanly.
    if (_netManager.streaming) [_netManager stopStreaming];
}

- (void)appActive:(NSNotification *)n {
    [_captureEngine ensureRunning];
    _ipLabel.text = [NSString stringWithFormat:@"IP %@", [OCNetManager localIPv4] ?: @"—"];
}

#pragma mark UI construction

- (UIColor *)panelColor { return [UIColor colorWithWhite:0.08 alpha:0.96]; }

- (UIButton *)makeButton:(NSString *)title frame:(CGRect)frame action:(SEL)action {
    UIButton *b = [UIButton buttonWithType:UIButtonTypeSystem];
    b.frame = frame;
    [b setTitle:title forState:UIControlStateNormal];
    b.titleLabel.font = [UIFont systemFontOfSize:14 weight:UIFontWeightSemibold];
    b.tintColor = [UIColor whiteColor];
    b.backgroundColor = [UIColor colorWithWhite:0.18 alpha:1.0];
    b.layer.cornerRadius = 8.0;
    [b addTarget:self action:action forControlEvents:UIControlEventTouchUpInside];
    return b;
}

- (UILabel *)makeLabel:(NSString *)text font:(UIFont *)font color:(UIColor *)color {
    UILabel *l = [[UILabel alloc] init];
    l.text = text;
    l.font = font;
    l.textColor = color;
    l.backgroundColor = [UIColor clearColor];
    return l;
}

- (void)buildUI {
    // --- Top HUD
    _statsLabel = [self makeLabel:@"idle"
                             font:[UIFont monospacedDigitSystemFontOfSize:11 weight:UIFontWeightRegular]
                            color:[UIColor colorWithWhite:1.0 alpha:0.85]];
    _statsLabel.numberOfLines = 4;
    _statsLabel.shadowColor = [UIColor blackColor];
    _statsLabel.shadowOffset = CGSizeMake(0, 1);
    [self.view addSubview:_statsLabel];

    _statusLabel = [self makeLabel:@"no PC"
                              font:[UIFont systemFontOfSize:12 weight:UIFontWeightMedium]
                             color:OCAccent()];
    _statusLabel.textAlignment = NSTextAlignmentCenter;
    [self.view addSubview:_statusLabel];

    _ipLabel = [self makeLabel:@"IP —"
                          font:[UIFont monospacedDigitSystemFontOfSize:11 weight:UIFontWeightRegular]
                         color:[UIColor colorWithWhite:1.0 alpha:0.6]];
    _ipLabel.textAlignment = NSTextAlignmentRight;
    _ipLabel.text = [NSString stringWithFormat:@"IP %@", [OCNetManager localIPv4] ?: @"—"];
    [self.view addSubview:_ipLabel];

    // --- WYSIWYG Metal preview (shown instead of the preview layer when filters are active)
    if (_pipeline.metalDevice != nil) {
        _rgbSpace = CGColorSpaceCreateDeviceRGB();
        _mtkView = [[MTKView alloc] initWithFrame:self.view.bounds device:_pipeline.metalDevice];
        _mtkView.colorPixelFormat = MTLPixelFormatBGRA8Unorm;
        _mtkView.framebufferOnly = NO; // CIContext must render into the drawable
        // BUG 1 diagnostics: must print 0 — YES would make the preview render
        // (CIContext render:toMTLTexture:) fail on every drawable.
        NSLog(@"[OCViewController] MTKView framebufferOnly=%d (must be 0)",
              (int)_mtkView.framebufferOnly);
        _mtkView.paused = YES;
        _mtkView.enableSetNeedsDisplay = YES; // we drive redraws per captured frame
        _mtkView.delegate = self;
        _mtkView.hidden = YES;
        _mtkView.backgroundColor = [UIColor blackColor];
        [self.view addSubview:_mtkView];
    }

    // --- Bottom control stack (frames set in viewDidLayoutSubviews)
    _startButton = [self makeButton:@"START" frame:CGRectZero action:@selector(startStopTapped:)];
    _startButton.backgroundColor = [UIColor colorWithWhite:0.14 alpha:1.0];
    _startButton.layer.borderWidth = 2.0;
    _startButton.layer.borderColor = OCAccent().CGColor;
    _startButton.titleLabel.font = [UIFont systemFontOfSize:17 weight:UIFontWeightBold];
    [self.view addSubview:_startButton];

    _cameraButton = [self makeButton:@"BACK" frame:CGRectZero action:@selector(cameraTapped:)];
    [self.view addSubview:_cameraButton];

    _torchButton = [self makeButton:@"TORCH" frame:CGRectZero action:@selector(torchTapped:)];
    [self.view addSubview:_torchButton];

    _resControl = [[UISegmentedControl alloc] initWithItems:@[@"720p", @"1080p"]];
    _resControl.selectedSegmentIndex = 0;
    _resControl.tintColor = [UIColor whiteColor];
    [_resControl addTarget:self action:@selector(resChanged:) forControlEvents:UIControlEventValueChanged];
    [self.view addSubview:_resControl];

    _bitrateLabel = [self makeLabel:@"— kbps"
                               font:[UIFont monospacedDigitSystemFontOfSize:12 weight:UIFontWeightRegular]
                              color:[UIColor whiteColor]];
    [self.view addSubview:_bitrateLabel];

    UILabel *abrLabel = [self makeLabel:@"ABR" font:[UIFont systemFontOfSize:11] color:[UIColor colorWithWhite:1 alpha:0.7]];
    abrLabel.tag = 771;
    [self.view addSubview:abrLabel];
    _abrSwitch = [[UISwitch alloc] init];
    _abrSwitch.on = YES;
    _abrSwitch.onTintColor = OCAccent();
    [_abrSwitch addTarget:self action:@selector(abrToggled:) forControlEvents:UIControlEventValueChanged];
    [self.view addSubview:_abrSwitch];

    _zoomValueLabel = [self makeLabel:@"1.0x"
                                 font:[UIFont monospacedDigitSystemFontOfSize:12 weight:UIFontWeightRegular]
                                color:[UIColor whiteColor]];
    [self.view addSubview:_zoomValueLabel];

    _cameraZoomSlider = [[UISlider alloc] init];
    _cameraZoomSlider.minimumValue = 1.0;
    _cameraZoomSlider.maximumValue = 8.0;
    _cameraZoomSlider.value = 1.0;
    _cameraZoomSlider.minimumTrackTintColor = OCAccent();
    [_cameraZoomSlider addTarget:self action:@selector(cameraZoomChanged:) forControlEvents:UIControlEventValueChanged];
    [self.view addSubview:_cameraZoomSlider];

    _panelToggleButton = [self makeButton:@"FILTERS  \u25B2" frame:CGRectZero action:@selector(panelToggled:)];
    [self.view addSubview:_panelToggleButton];

    [self buildFilterPanel];
}

#pragma mark Filter panel

- (CGFloat)addSectionHeader:(NSString *)title y:(CGFloat)y {
    UILabel *l = [self makeLabel:title.uppercaseString
                            font:[UIFont systemFontOfSize:11 weight:UIFontWeightBold]
                           color:OCAccent()];
    l.frame = CGRectMake(6, y, 380, 16);
    [_panelScroll addSubview:l];
    return y + 20;
}

- (CGFloat)addSliderRow:(NSString *)title tag:(OCSliderTag)tag min:(float)mn max:(float)mx y:(CGFloat)y {
    UILabel *l = [self makeLabel:title font:[UIFont systemFontOfSize:12] color:[UIColor colorWithWhite:1 alpha:0.85]];
    l.frame = CGRectMake(6, y + 6, 108, 16);
    [_panelScroll addSubview:l];

    UISlider *s = [[UISlider alloc] initWithFrame:CGRectMake(118, y, 260, 28)];
    s.minimumValue = mn;
    s.maximumValue = mx;
    s.tag = tag;
    s.minimumTrackTintColor = OCAccent();
    [s addTarget:self action:@selector(panelSliderChanged:) forControlEvents:UIControlEventValueChanged];
    [_panelScroll addSubview:s];
    _sliderByTag[@(tag)] = s;

    UILabel *v = [self makeLabel:@"" font:[UIFont monospacedDigitSystemFontOfSize:11 weight:UIFontWeightRegular] color:[UIColor whiteColor]];
    v.frame = CGRectMake(382, y + 6, 62, 16);
    v.tag = tag;
    v.textAlignment = NSTextAlignmentRight;
    [_panelScroll addSubview:v];
    _valueByTag[@(tag)] = v;
    return y + 34;
}

- (CGFloat)addButtonGrid:(NSArray<NSString *> *)titles
                 buttons:(NSMutableArray<UIButton *> *)out
                  action:(SEL)action
                       y:(CGFloat)y {
    CGFloat cw = 92, gap = 6;
    for (NSUInteger i = 0; i < titles.count; i++) {
        NSUInteger row = i / 4, col = i % 4;
        UIButton *b = [self makeButton:titles[i]
                                 frame:CGRectMake(6 + col * (cw + gap), y + row * 34, cw, 28)
                                action:action];
        b.tag = i;
        b.titleLabel.font = [UIFont systemFontOfSize:12 weight:UIFontWeightMedium];
        b.adjustsImageWhenHighlighted = NO; // iOS 12 spelling of the property
        [_panelScroll addSubview:b];
        [out addObject:b];
    }
    return y + ((titles.count + 3) / 4) * 34 + 4;
}

- (void)buildFilterPanel {
    _panel = [[UIView alloc] init];
    _panel.backgroundColor = [self panelColor];
    _panel.hidden = YES;
    _panel.layer.cornerRadius = 12.0;
    _panel.layer.masksToBounds = YES;
    [self.view addSubview:_panel];

    _panelScroll = [[UIScrollView alloc] init];
    _panelScroll.showsVerticalScrollIndicator = YES;
    _panelScroll.alwaysBounceVertical = YES;
    [_panel addSubview:_panelScroll];

    CGFloat y = 10;
    CGFloat w = 450.0; // panel content width (portrait-safe)

    y = [self addSectionHeader:@"Look" y:y];
    y = [self addButtonGrid:[OCFilterState validLooks] buttons:_lookButtons action:@selector(lookTapped:) y:y];

    y = [self addSectionHeader:@"Adjust" y:y];
    y = [self addSliderRow:@"Brightness" tag:OCSliderBrightness min:-1 max:1 y:y];
    y = [self addSliderRow:@"Contrast" tag:OCSliderContrast min:-1 max:1 y:y];
    y = [self addSliderRow:@"Saturation" tag:OCSliderSaturation min:0 max:2 y:y];
    y = [self addSliderRow:@"Temperature" tag:OCSliderTemperature min:-1 max:1 y:y];
    y = [self addSliderRow:@"Vibrance" tag:OCSliderVibrance min:0 max:1 y:y];
    y = [self addSliderRow:@"Gamma" tag:OCSliderGamma min:0.2 max:3 y:y];
    y = [self addSliderRow:@"Sharpness" tag:OCSliderSharpness min:0 max:1 y:y];
    y = [self addSliderRow:@"Vignette" tag:OCSliderVignette min:0 max:1 y:y];

    y = [self addSectionHeader:@"Beauty" y:y];
    y = [self addSliderRow:@"Skin smooth" tag:OCSliderBeauty min:0 max:1 y:y];

    y = [self addSectionHeader:@"Stylize" y:y];
    y = [self addButtonGrid:[OCFilterState validStylizes] buttons:_stylizeButtons action:@selector(stylizeTapped:) y:y];
    y = [self addSliderRow:@"Amount" tag:OCSliderStylizeAmount min:0 max:1 y:y];

    y = [self addSectionHeader:@"Geometry" y:y];
    UILabel *mirrorLabel = [self makeLabel:@"Mirror" font:[UIFont systemFontOfSize:12] color:[UIColor colorWithWhite:1 alpha:0.85]];
    mirrorLabel.frame = CGRectMake(6, y + 4, 108, 16);
    [_panelScroll addSubview:mirrorLabel];
    _mirrorSwitch = [[UISwitch alloc] initWithFrame:CGRectMake(118, y, 51, 31)];
    _mirrorSwitch.onTintColor = OCAccent();
    [_mirrorSwitch addTarget:self action:@selector(mirrorChanged:) forControlEvents:UIControlEventValueChanged];
    [_panelScroll addSubview:_mirrorSwitch];

    UILabel *flipLabel = [self makeLabel:@"Flip V" font:[UIFont systemFontOfSize:12] color:[UIColor colorWithWhite:1 alpha:0.85]];
    flipLabel.frame = CGRectMake(190, y + 4, 60, 16);
    [_panelScroll addSubview:flipLabel];
    _flipSwitch = [[UISwitch alloc] initWithFrame:CGRectMake(252, y, 51, 31)];
    _flipSwitch.onTintColor = OCAccent();
    [_flipSwitch addTarget:self action:@selector(flipChanged:) forControlEvents:UIControlEventValueChanged];
    [_panelScroll addSubview:_flipSwitch];
    y += 40;

    UILabel *rotLabel = [self makeLabel:@"Rotate" font:[UIFont systemFontOfSize:12] color:[UIColor colorWithWhite:1 alpha:0.85]];
    rotLabel.frame = CGRectMake(6, y + 6, 108, 16);
    [_panelScroll addSubview:rotLabel];
    _rotateControl = [[UISegmentedControl alloc] initWithItems:@[@"0", @"90", @"180", @"270"]];
    _rotateControl.frame = CGRectMake(118, y, 200, 28);
    _rotateControl.tintColor = [UIColor whiteColor];
    [_rotateControl addTarget:self action:@selector(rotateChanged:) forControlEvents:UIControlEventValueChanged];
    [_panelScroll addSubview:_rotateControl];
    y += 36;

    y = [self addSliderRow:@"Zoom" tag:OCSliderGeoZoom min:1 max:3 y:y];
    y = [self addSliderRow:@"Pan X" tag:OCSliderPanX min:-1 max:1 y:y];
    y = [self addSliderRow:@"Pan Y" tag:OCSliderPanY min:-1 max:1 y:y];

    UILabel *aspectLabel = [self makeLabel:@"Aspect" font:[UIFont systemFontOfSize:12] color:[UIColor colorWithWhite:1 alpha:0.85]];
    aspectLabel.frame = CGRectMake(6, y + 6, 108, 16);
    [_panelScroll addSubview:aspectLabel];
    _aspectControl = [[UISegmentedControl alloc] initWithItems:@[@"Native", @"16:9", @"4:3"]];
    _aspectControl.frame = CGRectMake(118, y, 240, 28);
    _aspectControl.tintColor = [UIColor whiteColor];
    [_aspectControl addTarget:self action:@selector(aspectChanged:) forControlEvents:UIControlEventValueChanged];
    [_panelScroll addSubview:_aspectControl];
    y += 36;

    y = [self addSectionHeader:@"LUT (.cube)" y:y];
    UIButton *importBtn = [self makeButton:@"Import .cube…" frame:CGRectMake(6, y, 150, 30) action:@selector(importLutTapped:)];
    [_panelScroll addSubview:importBtn];
    UIButton *clearBtn = [self makeButton:@"Clear LUT" frame:CGRectMake(164, y, 110, 30) action:@selector(clearLutTapped:)];
    [_panelScroll addSubview:clearBtn];
    y += 36;

    _lutList = [[UIView alloc] initWithFrame:CGRectMake(0, y, w, 0)];
    [_panelScroll addSubview:_lutList];
    y += 8;
    [self rebuildLutList:&y];

    y = [self addSectionHeader:@"Overlay" y:y];
    _overlayField = [[UITextField alloc] initWithFrame:CGRectMake(6, y, 300, 30)];
    _overlayField.placeholder = @"Overlay text";
    _overlayField.textColor = [UIColor whiteColor];
    _overlayField.font = [UIFont systemFontOfSize:13];
    _overlayField.backgroundColor = [UIColor colorWithWhite:0.2 alpha:1];
    _overlayField.borderStyle = UITextBorderStyleRoundedRect;
    _overlayField.delegate = self;
    _overlayField.returnKeyType = UIReturnKeyDone;
    [_overlayField addTarget:self action:@selector(overlayEdited:) forControlEvents:UIControlEventEditingChanged];
    [_panelScroll addSubview:_overlayField];

    UILabel *tcLabel = [self makeLabel:@"UTC timecode" font:[UIFont systemFontOfSize:12] color:[UIColor colorWithWhite:1 alpha:0.85]];
    tcLabel.frame = CGRectMake(6, y + 42, 108, 16);
    [_panelScroll addSubview:tcLabel];
    _timecodeSwitch = [[UISwitch alloc] initWithFrame:CGRectMake(118, y + 34, 51, 31)];
    _timecodeSwitch.onTintColor = OCAccent();
    [_timecodeSwitch addTarget:self action:@selector(timecodeChanged:) forControlEvents:UIControlEventValueChanged];
    [_panelScroll addSubview:_timecodeSwitch];
    y += 74;

    UIButton *reset = [self makeButton:@"RESET ALL" frame:CGRectMake(6, y, w - 24, 34) action:@selector(resetTapped:)];
    reset.layer.borderColor = [UIColor redColor].CGColor;
    reset.layer.borderWidth = 1.0;
    [_panelScroll addSubview:reset];
    y += 46;

    _panelScroll.contentSize = CGSizeMake(w, y);
}

// Appends one button per .cube in Documents; advances *y.
- (void)rebuildLutList:(CGFloat *)y {
    for (UIView *v in _lutList.subviews) [v removeFromSuperview];
    NSArray<NSString *> *names = [OCLutLoader availableLutNames];
    CGFloat ly = 0;
    for (NSUInteger i = 0; i < names.count && i < 12; i++) {
        UIButton *b = [self makeButton:names[i] frame:CGRectMake(6, ly, 340, 28) action:@selector(lutTapped:)];
        b.contentHorizontalAlignment = UIControlContentHorizontalAlignmentLeft;
        b.titleEdgeInsets = UIEdgeInsetsMake(0, 8, 0, 0);
        b.titleLabel.font = [UIFont systemFontOfSize:11 weight:UIFontWeightRegular];
        b.tag = i;
        [_lutList addSubview:b];
        ly += 32;
    }
    _lutList.frame = CGRectMake(0, _lutList.frame.origin.y, _lutList.frame.size.width, ly);
    if (y) *y += ly + 8;
}

- (void)refreshLutList {
    CGFloat y = _lutList.frame.origin.y;
    [self rebuildLutList:&y]; // rebuildLutList advances y past the list
    _panelScroll.contentSize = CGSizeMake(450.0, MAX(y, _panelScroll.contentSize.height));
}

#pragma mark Layout

- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    CGRect b = self.view.bounds;
    _previewLayer.frame = b;
    _mtkView.frame = b;

    CGFloat safeTop = 24.0;
    CGFloat barH = 162.0;

    _statsLabel.frame = CGRectMake(10, safeTop, b.size.width * 0.62, 64);
    _ipLabel.frame = CGRectMake(b.size.width * 0.66, safeTop, b.size.width * 0.34 - 10, 16);
    _statusLabel.frame = CGRectMake(b.size.width * 0.2, safeTop + 66, b.size.width * 0.6, 16);

    CGFloat by = b.size.height - barH - 8;
    CGFloat bw = b.size.width;

    _startButton.frame = CGRectMake(12, by, 108, 44);
    _cameraButton.frame = CGRectMake(128, by, 78, 44);
    _torchButton.frame = CGRectMake(214, by, 72, 44);
    _abrSwitch.frame = CGRectMake(bw - 66, by + 6, 51, 31);
    UILabel *abrL = (UILabel *)[self.view viewWithTag:771];
    abrL.frame = CGRectMake(bw - 106, by + 12, 36, 16);
    _bitrateLabel.frame = CGRectMake(296, by + 14, bw - 410, 16);

    _resControl.frame = CGRectMake(12, by + 52, 170, 30);
    _zoomValueLabel.frame = CGRectMake(190, by + 56, 42, 16);
    _cameraZoomSlider.frame = CGRectMake(236, by + 52, bw - 248, 30);

    _panelToggleButton.frame = CGRectMake(12, by + 90, bw - 24, 40);

    CGFloat panelH = _panelOpen ? MIN(b.size.height - safeTop - barH - 30, 430) : 0;
    CGFloat pw = MIN(450.0, bw - 16);
    _panel.frame = CGRectMake((bw - pw) / 2.0, by - panelH - 6, pw, panelH);
    _panelScroll.frame = _panel.bounds;
    _panel.hidden = !_panelOpen || panelH < 10;
}

- (UIInterfaceOrientationMask)supportedInterfaceOrientations {
    // plist owner: UISupportedInterfaceOrientations must include Portrait +
    // LandscapeLeft + LandscapeRight to match.
    return UIInterfaceOrientationMaskAllButUpsideDown;
}

- (BOOL)prefersStatusBarHidden {
    return YES;
}

#pragma mark Filter state plumbing

- (void)updateFilterState:(void (NS_NOESCAPE ^)(OCFilterState *s))block {
    [_filterState performUpdate:block];
}

- (void)filterChanged:(NSNotification *)n {
    [self updatePreviewMode];
    [self refreshButtonStates];
}

- (void)lutsChanged:(NSNotification *)n {
    [_pipeline clearLutCache];
    [self refreshLutList];
    [self refreshButtonStates];
}

- (void)updatePreviewMode {
    // WYSIWYG MTKView replaces the raw preview layer only while filters are active.
    BOOL identity = _filterState.isIdentity;
    BOOL metalOk = (_mtkView != nil);
    BOOL hideMtk = identity || !metalOk;
    BOOL hideLayer = metalOk ? !identity : NO;
    if (_mtkView.hidden != hideMtk) _mtkView.hidden = hideMtk;
    if (_previewLayer.hidden != hideLayer) _previewLayer.hidden = hideLayer;
    if (!_mtkView.hidden) [_mtkView setNeedsDisplay];
}

- (void)applyStateToUI {
    OCFilterState *s = _filterState;
    NSArray<NSString *> *looks = [OCFilterState validLooks];
    NSUInteger li = [looks indexOfObject:s.look];
    for (NSUInteger i = 0; i < _lookButtons.count; i++) {
        _lookButtons[i].backgroundColor = (i == li) ? OCAccent() : [UIColor colorWithWhite:0.18 alpha:1.0];
    }
    NSArray<NSString *> *sz = [OCFilterState validStylizes];
    NSUInteger si = [sz indexOfObject:s.stylize];
    for (NSUInteger i = 0; i < _stylizeButtons.count; i++) {
        _stylizeButtons[i].backgroundColor = (i == si) ? OCAccent() : [UIColor colorWithWhite:0.18 alpha:1.0];
    }

    NSDictionary<NSNumber *, NSNumber *> *values = @{
        @(OCSliderBrightness) : @(s.brightness),
        @(OCSliderContrast) : @(s.contrast),
        @(OCSliderSaturation) : @(s.saturation),
        @(OCSliderTemperature) : @(s.temperature),
        @(OCSliderVibrance) : @(s.vibrance),
        @(OCSliderGamma) : @(s.gamma),
        @(OCSliderSharpness) : @(s.sharpness),
        @(OCSliderVignette) : @(s.vignette),
        @(OCSliderBeauty) : @(s.beauty),
        @(OCSliderStylizeAmount) : @(s.stylizeAmount),
        @(OCSliderGeoZoom) : @(s.zoom),
        @(OCSliderPanX) : @(s.panX),
        @(OCSliderPanY) : @(s.panY),
    };
    [values enumerateKeysAndObjectsUsingBlock:^(NSNumber *tag, NSNumber *val, BOOL *stop) {
        UISlider *sl = _sliderByTag[tag];
        sl.value = (float)val.doubleValue;
        UILabel *vl = _valueByTag[tag];
        vl.text = [NSString stringWithFormat:@"%.2f", val.doubleValue];
    }];

    _mirrorSwitch.on = s.mirror;
    _flipSwitch.on = s.flipV;
    _rotateControl.selectedSegmentIndex = (NSUInteger)(s.rotate / 90);
    NSUInteger ai = [[OCFilterState validAspects] indexOfObject:s.aspect];
    _aspectControl.selectedSegmentIndex = (ai == NSNotFound) ? 0 : ai;
    _overlayField.text = s.overlayText;
    _timecodeSwitch.on = s.showTimecode;
}

- (void)refreshButtonStates {
    OCFilterState *s = _filterState;
    NSArray<NSString *> *looks = [OCFilterState validLooks];
    NSUInteger li = [looks indexOfObject:s.look];
    for (NSUInteger i = 0; i < _lookButtons.count; i++) {
        _lookButtons[i].backgroundColor = (i == li) ? OCAccent() : [UIColor colorWithWhite:0.18 alpha:1.0];
    }
    NSArray<NSString *> *sz = [OCFilterState validStylizes];
    NSUInteger si = [sz indexOfObject:s.stylize];
    for (NSUInteger i = 0; i < _stylizeButtons.count; i++) {
        _stylizeButtons[i].backgroundColor = (i == si) ? OCAccent() : [UIColor colorWithWhite:0.18 alpha:1.0];
    }
    NSArray<NSString *> *luts = [OCLutLoader availableLutNames];
    NSUInteger luti = s.lut ? [luts indexOfObject:s.lut] : NSNotFound;
    for (UIView *v in _lutList.subviews) {
        if ([v isKindOfClass:[UIButton class]]) {
            UIButton *b = (UIButton *)v;
            b.backgroundColor = (b.tag == (NSInteger)luti && luti != NSNotFound)
                ? OCAccent() : [UIColor colorWithWhite:0.18 alpha:1.0];
        }
    }
}

#pragma mark Control actions

- (void)startStopTapped:(UIButton *)sender {
    if (_netManager.streaming) {
        [_netManager stopStreaming];
        return;
    }
    if (!_netManager.clientConnected) {
        _statusLabel.text = @"no PC — waiting on TCP 9923";
        return;
    }
    sender.enabled = NO;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSError *err = nil;
        BOOL ok = [_netManager startStreamingToConnectedClientWidth:1280
                                                             height:720
                                                                fps:30
                                                               kbps:3000
                                                             keyint:60
                                                              error:&err];
        dispatch_async(dispatch_get_main_queue(), ^{
            _startButton.enabled = YES;
            if (!ok) {
                _statusLabel.text = err.localizedDescription ?: @"start failed";
            }
        });
    });
}

- (void)cameraTapped:(UIButton *)sender {
    NSString *target = [_captureEngine.activeCameraId isEqualToString:@"front"] ? @"back" : @"front";
    sender.enabled = NO;
    [_captureEngine switchToCameraId:target completion:^(NSString *activeId, NSError *err) {
        dispatch_async(dispatch_get_main_queue(), ^{
            sender.enabled = YES;
            if (err) _statusLabel.text = err.localizedDescription;
            [self refreshCameraControls];
        });
    }];
}

- (void)torchTapped:(UIButton *)sender {
    BOOL applied = [_captureEngine applyTorchOn:!_captureEngine.torchOn];
    [sender setTitle:applied ? @"TORCH\u2713" : @"TORCH" forState:UIControlStateNormal];
}

- (void)resChanged:(UISegmentedControl *)seg {
    BOOL hd = (seg.selectedSegmentIndex == 1);
    [_captureEngine setWantsHighResolution:hd];
    [_netManager setBitrateCeilingKbps:hd ? 6000 : 3000]; // §6 defaults per resolution
}

- (void)abrToggled:(UISwitch *)sw {
    [_netManager enableAutoBitrate:sw.on];
}

- (void)cameraZoomChanged:(UISlider *)slider {
    [_captureEngine setZoomFactor:slider.value];
    _zoomValueLabel.text = [NSString stringWithFormat:@"%.1fx", slider.value];
}

- (void)panelToggled:(UIButton *)sender {
    _panelOpen = !_panelOpen;
    [sender setTitle:_panelOpen ? @"FILTERS  \u25BC" : @"FILTERS  \u25B2"
           forState:UIControlStateNormal];
    [self.view setNeedsLayout];
    [self.view layoutIfNeeded];
    [self refreshLutList];
}

- (void)refreshCameraControls {
    BOOL front = [_captureEngine.activeCameraId isEqualToString:@"front"];
    [_cameraButton setTitle:front ? @"FRONT" : @"BACK" forState:UIControlStateNormal];
    _torchButton.enabled = _captureEngine.torchSupported;
    [_torchButton setTitle:_captureEngine.torchOn ? @"TORCH\u2713" : @"TORCH" forState:UIControlStateNormal];
    [_resControl setEnabled:!front forSegmentAtIndex:1];
    if (front && _resControl.selectedSegmentIndex == 1) {
        _resControl.selectedSegmentIndex = 0;
        [_captureEngine setWantsHighResolution:NO];
    }
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        CGFloat maxZ = _captureEngine.maxZoomFactor;
        dispatch_async(dispatch_get_main_queue(), ^{
            _cameraZoomSlider.maximumValue = (float)maxZ;
            _cameraZoomSlider.value = (float)_captureEngine.zoomFactor;
            _zoomValueLabel.text = [NSString stringWithFormat:@"%.1fx", _captureEngine.zoomFactor];
        });
    });
}

#pragma mark Panel actions

- (void)lookTapped:(UIButton *)sender {
    [self updateFilterState:^(OCFilterState *s) {
        s.look = [OCFilterState validLooks][sender.tag];
    }];
}

- (void)stylizeTapped:(UIButton *)sender {
    [self updateFilterState:^(OCFilterState *s) {
        s.stylize = [OCFilterState validStylizes][sender.tag];
    }];
}

- (void)lutTapped:(UIButton *)sender {
    NSArray<NSString *> *names = [OCLutLoader availableLutNames];
    if (sender.tag >= (NSInteger)names.count) return;
    NSString *name = names[sender.tag];
    BOOL turningOff = [name isEqualToString:_filterState.lut];
    [self updateFilterState:^(OCFilterState *s) {
        s.lut = turningOff ? nil : name;
    }];
}

- (void)clearLutTapped:(UIButton *)sender {
    [self updateFilterState:^(OCFilterState *s) {
        s.lut = nil;
    }];
}

- (void)panelSliderChanged:(UISlider *)slider {
    float v = slider.value;
    UILabel *vl = _valueByTag[@(slider.tag)];
    vl.text = [NSString stringWithFormat:@"%.2f", v];
    switch ((OCSliderTag)slider.tag) {
        case OCSliderBrightness:   [self updateFilterState:^(OCFilterState *s) { s.brightness = v; }]; break;
        case OCSliderContrast:     [self updateFilterState:^(OCFilterState *s) { s.contrast = v; }]; break;
        case OCSliderSaturation:   [self updateFilterState:^(OCFilterState *s) { s.saturation = v; }]; break;
        case OCSliderTemperature:  [self updateFilterState:^(OCFilterState *s) { s.temperature = v; }]; break;
        case OCSliderVibrance:     [self updateFilterState:^(OCFilterState *s) { s.vibrance = v; }]; break;
        case OCSliderGamma:        [self updateFilterState:^(OCFilterState *s) { s.gamma = v; }]; break;
        case OCSliderSharpness:    [self updateFilterState:^(OCFilterState *s) { s.sharpness = v; }]; break;
        case OCSliderVignette:     [self updateFilterState:^(OCFilterState *s) { s.vignette = v; }]; break;
        case OCSliderBeauty:       [self updateFilterState:^(OCFilterState *s) { s.beauty = v; }]; break;
        case OCSliderStylizeAmount:[self updateFilterState:^(OCFilterState *s) { s.stylizeAmount = v; }]; break;
        case OCSliderGeoZoom:      [self updateFilterState:^(OCFilterState *s) { s.zoom = v; }]; break;
        case OCSliderPanX:         [self updateFilterState:^(OCFilterState *s) { s.panX = v; }]; break;
        case OCSliderPanY:         [self updateFilterState:^(OCFilterState *s) { s.panY = v; }]; break;
        default: break;
    }
}

- (void)mirrorChanged:(UISwitch *)sw { [self updateFilterState:^(OCFilterState *s) { s.mirror = sw.on; }]; }
- (void)flipChanged:(UISwitch *)sw   { [self updateFilterState:^(OCFilterState *s) { s.flipV = sw.on; }]; }

- (void)rotateChanged:(UISegmentedControl *)seg {
    NSInteger deg = seg.selectedSegmentIndex * 90; // 0/90/180/270
    [self updateFilterState:^(OCFilterState *s) { s.rotate = deg; }];
}

- (void)aspectChanged:(UISegmentedControl *)seg {
    NSString *a = [OCFilterState validAspects][seg.selectedSegmentIndex];
    [self updateFilterState:^(OCFilterState *s) { s.aspect = a; }];
}

- (void)overlayEdited:(UITextField *)field {
    NSString *t = field.text ?: @"";
    [self updateFilterState:^(OCFilterState *s) { s.overlayText = t; }];
}

- (void)timecodeChanged:(UISwitch *)sw {
    [self updateFilterState:^(OCFilterState *s) { s.showTimecode = sw.on; }];
}

- (void)resetTapped:(UIButton *)sender {
    [_filterState reset];
    [self applyStateToUI];
}

- (void)importLutTapped:(UIButton *)sender {
    // iOS 8 document-picker API; .cube has no UTI so accept generic data.
    UIDocumentPickerViewController *dp =
        [[UIDocumentPickerViewController alloc] initWithDocumentTypes:@[@"public.data"]
                                                               inMode:UIDocumentPickerModeImport];
    dp.delegate = self;
    dp.allowsMultipleSelection = NO;
    [self presentViewController:dp animated:YES completion:nil];
}

- (void)documentPicker:(UIDocumentPickerViewController *)controller
didPickDocumentsAtURLs:(NSArray<NSURL *> *)urls {
    NSURL *url = urls.firstObject;
    if (!url) return;
    NSError *err = nil;
    if ([OCLutLoader importCubeFileAtURL:url error:&err]) {
        _statusLabel.text = [NSString stringWithFormat:@"LUT imported: %@", url.lastPathComponent];
    } else {
        _statusLabel.text = err.localizedDescription ?: @"LUT import failed";
    }
}

- (BOOL)textFieldShouldReturn:(UITextField *)textField {
    [textField resignFirstResponder];
    return YES;
}

#pragma mark Streaming / net delegate (main thread)

- (void)netManagerClientDidConnect:(OCNetManager *)manager name:(NSString *)name {
    _statusLabel.text = [NSString stringWithFormat:@"PC: %@", name ?: @"connected"];
}

- (void)netManagerClientDidDisconnect:(OCNetManager *)manager {
    _statusLabel.text = @"no PC";
}

- (void)netManagerStreamingStateDidChange:(OCNetManager *)manager {
    BOOL streaming = manager.streaming;
    [_startButton setTitle:streaming ? @"STOP" : @"START" forState:UIControlStateNormal];
    _startButton.backgroundColor = streaming
        ? [UIColor colorWithRed:0.75 green:0.15 blue:0.15 alpha:1.0]
        : [UIColor colorWithWhite:0.14 alpha:1.0];
    [UIApplication sharedApplication].idleTimerDisabled = streaming; // keep screen on while streaming
    _statusLabel.text = streaming
        ? [NSString stringWithFormat:@"live → %@", manager.clientAddress ?: @""]
        : (_netManager.clientConnected ? @"PC connected" : @"no PC");
    if (!streaming) {
        _statsLabel.text = @"idle";
        _bitrateLabel.text = @"";
    }
}

- (void)netManager:(OCNetManager *)manager activeCameraDidChange:(NSString *)cameraId {
    [self refreshCameraControls];
}

- (void)netManager:(OCNetManager *)manager didReceiveRemoteFilterState:(OCFilterState *)state {
    [self applyStateToUI]; // state object already mutated; sync the controls
    [self updatePreviewMode];
}

- (void)netManager:(OCNetManager *)manager didUpdateStatsFps:(double)fps
                                     kbps:(double)kbps encMs:(double)encMs
                                  lossPct:(double)lossPct nacks:(uint32_t)nacks
                                     sent:(uint32_t)sent {
    _statsLabel.text = [NSString stringWithFormat:
        @"fps %.1f\n%.0f kbps  enc %.1f ms\nloss %.1f%%  nack %u\nsent %u",
        fps, kbps, encMs, lossPct, nacks, sent];
    _bitrateLabel.text = [NSString stringWithFormat:@"%@%.0f kbps",
                          manager.abrAuto ? @"auto " : @"", kbps];
}

- (void)netManager:(OCNetManager *)manager didFailWithMessage:(NSString *)message {
    _statusLabel.text = message;
    [self showAlert:@"Network error" message:message];
}

- (void)showAlert:(NSString *)title message:(NSString *)message {
    UIAlertController *a = [UIAlertController alertControllerWithTitle:title
                                                               message:message
                                                        preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"OK" style:UIAlertActionStyleDefault handler:nil]];
    [self presentViewController:a animated:YES completion:nil];
}

#pragma mark OCFilterPipelinePreviewDelegate (main thread by contract)

- (void)filterPipelineDidRenderFrame:(OCFilterPipeline *)pipeline {
    if (!_mtkView.hidden) [_mtkView setNeedsDisplay];
}

#pragma mark MTKViewDelegate

- (void)mtkView:(MTKView *)view drawableSizeWillChange:(CGSize)size {}

- (void)drawInMTKView:(MTKView *)view {
    id<CAMetalDrawable> drawable = view.currentDrawable;
    if (!drawable) return;
    id<MTLCommandBuffer> cb = [_pipeline.commandQueue commandBuffer];
    if (!cb) return;

    @try {
        CGSize ds = view.drawableSize;
        CIImage *img = [_pipeline lastPreviewImage];
        if (img && !CGRectIsEmpty(img.extent)) {
            // Letterbox-fit the landscape frame into the drawable.
            CGRect ext = img.extent;
            CGFloat s = MIN(ds.width / ext.size.width, ds.height / ext.size.height);
            CGAffineTransform t = CGAffineTransformMakeTranslation((ds.width - ext.size.width * s) / 2.0,
                                                                   (ds.height - ext.size.height * s) / 2.0);
            t = CGAffineTransformScale(t, s, s);
            t = CGAffineTransformTranslate(t, -ext.origin.x, -ext.origin.y);
            img = [img imageByApplyingTransform:t];
            [_pipeline.ciContext render:img
                           toMTLTexture:drawable.texture
                          commandBuffer:cb
                                 bounds:CGRectMake(0, 0, ds.width, ds.height)
                             colorSpace:_rgbSpace];
        } else {
            MTLRenderPassDescriptor *rpd = [MTLRenderPassDescriptor renderPassDescriptor];
            rpd.colorAttachments[0].texture = drawable.texture;
            rpd.colorAttachments[0].loadAction = MTLLoadActionClear;
            rpd.colorAttachments[0].storeAction = MTLStoreActionStore;
            rpd.colorAttachments[0].clearColor = MTLClearColorMake(0.0, 0.0, 0.0, 1.0);
            id<MTLRenderCommandEncoder> enc = [cb renderCommandEncoderWithDescriptor:rpd];
            [enc endEncoding];
        }
        [cb presentDrawable:drawable];
        [cb commit];
    } @catch (NSException *ex) {
        // Last Exception Backtrace in device IPS was on the main thread through
        // UIKit + OmniCam; a CI KVC throw during preview must not abort the app.
        static BOOL logged = NO;
        if (!logged) {
            logged = YES;
            NSLog(@"[OmniCam] drawInMTKView exception: %@", ex);
        }
    }
}

@end
