import AppKit
import QuartzCore

/// The layer-backed content view for the overlay window. Hosts a single
/// container `CALayer` — the "agent cursor composite" — made of a wide
/// radial cyan bloom, a gradient-filled classic cursor arrow, and a
/// white outline stroke. Tinted distinctly from the system cursor so the
/// user can tell them apart at a glance.
///
/// Uses `isFlipped = true` so the view's coordinate system matches
/// global screen points (origin top-left, y increases downward). This
/// lets callers set `cursorLayer.position` directly from screen-point
/// coordinates without a per-update flip.
public final class AgentCursorView: NSView {
    public override var isFlipped: Bool { true }

    /// Animators drive this layer's `position` and `transform`. It's a
    /// container `CALayer` holding the bloom + stroke sublayers —
    /// setting `.position` here moves the whole composite as one unit.
    /// The layer's `anchorPoint` is (0.5, 0.5) so `position` is the
    /// cursor's visual center.
    public let cursorLayer: CALayer

    /// The radial bloom layer. Exposed so `AgentCursor` can animate its
    /// alpha envelope ("breath") during a glide without poking into the
    /// sublayer tree by index.
    public let bloomLayer: CAGradientLayer

    public override init(frame: NSRect) {
        let built = AgentCursorView.makeCursorLayer()
        self.cursorLayer = built.container
        self.bloomLayer = built.bloom
        super.init(frame: frame)
        wantsLayer = true
        let root = CALayer()
        root.backgroundColor = NSColor.clear.cgColor
        layer = root
        root.addSublayer(cursorLayer)
    }

    public required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported for AgentCursorView")
    }

    /// Position the cursor at a screen-point coordinate. Call from the
    /// main actor. Disables the implicit CA animation on `position`
    /// changes so instantaneous moves stay snappy; explicit animations
    /// are driven by the caller wrapping a `CATransaction` of their
    /// own around this call.
    public func setPosition(_ point: CGPoint) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        cursorLayer.position = point
        CATransaction.commit()
    }

    /// A classic cursor-arrow pointer (SVG-derived path, tip at upper-
    /// left) sitting inside a 60pt container. The container holds three
    /// layers, back → front:
    ///
    ///   1. Radial `CAGradientLayer` — 60×60pt cyan bloom, alpha falloff
    ///      center `0.55` → mid `0.15` → edge `0.0`. Wide, perfectly-
    ///      circular glow that reads as "agent presence" without tracking
    ///      the arrow silhouette.
    ///   2. Gradient-filled arrow — `CAGradientLayer` (axial 135°) masked
    ///      by a FILLED `CAShapeLayer` of the cursor path. Gradient
    ///      colors fill the entire arrow interior.
    ///   3. White outline stroke — `CAShapeLayer` with `strokeColor =
    ///      white`, 2pt line along the same path, centered. Half the
    ///      stroke extends outside the gradient silhouette, creating
    ///      the white border that defines the shape against any
    ///      background.
    ///
    /// The container's `anchorPoint` is `(0.5, 0.5)` so `layer.position`
    /// is the cursor's visual center. No container-level rotation — the
    /// SVG path already has the tip at upper-left.
    private static func makeCursorLayer() -> (container: CALayer, bloom: CAGradientLayer) {
        let style = AgentCursorStyle.default
        let containerSize = style.containerSize
        let shapeSize = style.shapeSize

        // Arrow path, scaled from the SVG 24-unit box to `shapeSize`
        // centered in the container. The SVG content only occupies ~18
        // of the 24 units (padding on each side); the effective drawn
        // size is therefore `shapeSize * 18/24` ≈ 75% of the frame.
        let shapeFrame = CGRect(
            x: (containerSize - shapeSize) / 2,
            y: (containerSize - shapeSize) / 2,
            width: shapeSize,
            height: shapeSize
        )
        let arrowPath = makeCursorArrowPath(in: shapeFrame)

        // --- 1. Radial bloom (back) ---
        let bloom = CAGradientLayer()
        bloom.type = .radial
        let bloomCenter = style.bloomColor
        bloom.colors = [
            bloomCenter.withAlphaComponent(style.bloomCenterAlpha).cgColor,
            bloomCenter.withAlphaComponent(style.bloomMidAlpha).cgColor,
            bloomCenter.withAlphaComponent(0.0).cgColor,
        ]
        bloom.locations = [0.0, 0.5, 1.0]
        bloom.startPoint = CGPoint(x: 0.5, y: 0.5)
        // For radial CAGradientLayer, `endPoint` sets the outer edge of
        // the gradient. (1.0, 1.0) takes the bloom to the layer's
        // bottom-right corner — a full 30pt radius from center.
        bloom.endPoint = CGPoint(x: 1.0, y: 1.0)
        bloom.frame = CGRect(x: 0, y: 0, width: containerSize, height: containerSize)

        // --- 2. Gradient-filled arrow (middle) ---
        // Filled mask: any opaque color works, the mask uses alpha. The
        // gradient layer above then fills the entire arrow interior.
        let fillMask = CAShapeLayer()
        fillMask.path = arrowPath
        fillMask.fillColor = NSColor.white.cgColor
        fillMask.strokeColor = NSColor.clear.cgColor
        fillMask.frame = CGRect(x: 0, y: 0, width: containerSize, height: containerSize)

        let fillGradient = CAGradientLayer()
        fillGradient.type = .axial
        fillGradient.colors = style.strokeGradientStops.map { $0.color.cgColor }
        fillGradient.locations = style.strokeGradientStops.map { NSNumber(value: Double($0.location)) }
        let angleRad: CGFloat = style.strokeGradientAngleDegrees * .pi / 180
        let dx = sin(angleRad) / 2
        let dy = -cos(angleRad) / 2
        fillGradient.startPoint = CGPoint(x: 0.5 - dx, y: 0.5 - dy)
        fillGradient.endPoint = CGPoint(x: 0.5 + dx, y: 0.5 + dy)
        fillGradient.frame = CGRect(x: 0, y: 0, width: containerSize, height: containerSize)
        fillGradient.mask = fillMask

        // --- 3. White outline stroke (front) ---
        // 2pt white stroke along the arrow path. Default CA strokes are
        // centered on the path — half extends outside the silhouette,
        // half inside — but the gradient fill behind covers the inside
        // half, so the visible effect is a clean outline around the
        // gradient shape.
        let border = CAShapeLayer()
        border.path = arrowPath
        border.fillColor = NSColor.clear.cgColor
        border.strokeColor = NSColor.white.cgColor
        border.lineWidth = style.strokeWidth
        border.lineJoin = .round
        border.lineCap = .round
        border.frame = CGRect(x: 0, y: 0, width: containerSize, height: containerSize)

        // --- Container ---
        // Anchor the container on the cursor's TIP — not the geometric
        // center — so `layer.position = clickPoint` lands the tip
        // exactly on the click, matching macOS system-cursor behavior.
        //
        // The tip in SVG coords is at ~(3.35, 3.35) in the 24-unit
        // viewBox (upper-left corner of the path). Scale into container
        // coords and convert to a normalized anchor.
        let svgTip = CGPoint(x: 3.35, y: 3.35)
        let shapeScale = shapeSize / 24.0
        let tipInShape = CGPoint(x: svgTip.x * shapeScale, y: svgTip.y * shapeScale)
        let tipInContainer = CGPoint(
            x: shapeFrame.origin.x + tipInShape.x,
            y: shapeFrame.origin.y + tipInShape.y
        )
        let anchor = CGPoint(
            x: tipInContainer.x / containerSize,
            y: tipInContainer.y / containerSize
        )

        let container = CALayer()
        container.bounds = CGRect(x: 0, y: 0, width: containerSize, height: containerSize)
        container.anchorPoint = anchor
        container.position = CGPoint(x: -100, y: -100)  // off-screen default
        container.addSublayer(bloom)
        container.addSublayer(fillGradient)
        container.addSublayer(border)
        // NO rotation — the SVG path already has the tip at upper-left.

        return (container, bloom)
    }

    /// Build the cursor-arrow path as a `CGPath`, scaled from the 24-unit
    /// SVG reference in `docs/_local/references/` into `frame`.
    ///
    /// Shape: a classic pointer arrow with the tip at upper-left and the
    /// tail extending to lower-right. All corners are rounded via short
    /// cubic beziers. At 15pt `shapeSize`, the visible content occupies
    /// about 11.5pt (the SVG content is ~18 units in a 24-unit viewport),
    /// so the drawn arrow reads larger thanks to the 2pt white outline
    /// that wraps it.
    ///
    /// Coordinates are in the container's coordinate system (isFlipped
    /// view, so +y is down — same convention as the SVG source).
    private static func makeCursorArrowPath(in frame: CGRect) -> CGPath {
        let path = CGMutablePath()

        // SVG viewBox is 24×24; scale all coords uniformly to `frame.width`.
        let s = frame.width / 24.0
        let ox = frame.minX
        let oy = frame.minY
        func pt(_ x: Double, _ y: Double) -> CGPoint {
            CGPoint(x: ox + CGFloat(x) * s, y: oy + CGFloat(y) * s)
        }

        // Path walk matches the SVG's M/C/L sequence. Tail corner first
        // (upper-right of the shape), then the tip corner (upper-left),
        // then the outer wing corner (lower-left), then the inner notch
        // where the tail meets the body.
        path.move(to: pt(20.5056, 10.7754))
        path.addCurve(to: pt(21.5176, 10.2459),
                      control1: pt(21.1225, 10.5355),
                      control2: pt(21.431, 10.4155))
        path.addCurve(to: pt(21.5115, 9.77954),
                      control1: pt(21.5926, 10.099),
                      control2: pt(21.5903, 9.92446))
        path.addCurve(to: pt(20.486, 9.2768),
                      control1: pt(21.4205, 9.61226),
                      control2: pt(21.109, 9.50044))
        path.addLine(to: pt(4.59629, 3.5728))
        path.addCurve(to: pt(3.66514, 3.35605),
                      control1: pt(4.0866, 3.38983),
                      control2: pt(3.83175, 3.29835))
        path.addCurve(to: pt(3.35629, 3.6649),
                      control1: pt(3.52029, 3.40621),
                      control2: pt(3.40645, 3.52004))
        path.addCurve(to: pt(3.57304, 4.59605),
                      control1: pt(3.29859, 3.8315),
                      control2: pt(3.39008, 4.08635))
        path.addLine(to: pt(9.277, 20.4858))
        path.addCurve(to: pt(9.77973, 21.5113),
                      control1: pt(9.50064, 21.1088),
                      control2: pt(9.61246, 21.4203))
        path.addCurve(to: pt(10.2461, 21.5174),
                      control1: pt(9.92465, 21.5901),
                      control2: pt(10.0991, 21.5924))
        path.addCurve(to: pt(10.7756, 20.5054),
                      control1: pt(10.4157, 21.4308),
                      control2: pt(10.5356, 21.1223))
        path.addLine(to: pt(13.3724, 13.8278))
        path.addCurve(to: pt(13.4792, 13.5957),
                      control1: pt(13.4194, 13.707),
                      control2: pt(13.4429, 13.6466))
        path.addCurve(to: pt(13.5959, 13.479),
                      control1: pt(13.5114, 13.5506),
                      control2: pt(13.5508, 13.5112))
        path.addCurve(to: pt(13.828, 13.3722),
                      control1: pt(13.6468, 13.4427),
                      control2: pt(13.7072, 13.4192))
        path.addLine(to: pt(20.5056, 10.7754))
        path.closeSubpath()
        return path
    }
}
