import AppKit
import QuartzCore

/// Named color stop for the agent-cursor's axial stroke gradient.
/// Pairing color + location keeps the spec's lavender stops grep-able
/// in one place — touch these values to retint the pointer.
public struct AgentCursorGradientStop: Sendable {
    public let color: NSColor
    public let location: CGFloat
    public init(color: NSColor, location: CGFloat) {
        self.color = color
        self.location = location
    }
}

/// Visual constants for the agent cursor — shape sizing, gradient
/// stops, bloom falloff, stroke widths. Hard-coded and grep-able per
/// the design spec (see `docs/_local/agent-cursor-redesign.md`). Not
/// exposed via `set_agent_cursor_motion`; a separate
/// `set_agent_cursor_style` tool can land later if per-session theming
/// becomes a real ask.
public struct AgentCursorStyle: Sendable {
    /// Container layer size. Grew 25pt → 60pt to hold the bloom
    /// without clipping. Window frame is unchanged — the container is
    /// visual only, no hit-test implication.
    public let containerSize: CGFloat

    /// Rounded-arrow shape size (tip-to-base). Unchanged from the
    /// previous triangle: 15pt reads as a cursor tip without feeling
    /// chunky.
    public let shapeSize: CGFloat

    /// Axial stroke gradient — lavender family. 135° rotation (set via
    /// `strokeGradientAngleDegrees`) puts the near-white stop at the
    /// top-left (tip) and the indigo stop at the bottom-right (tail).
    public let strokeGradientStops: [AgentCursorGradientStop]
    public let strokeGradientAngleDegrees: CGFloat

    public let strokeWidth: CGFloat
    public let highlightStrokeWidth: CGFloat

    /// Lavender bloom — single radial `CAGradientLayer` below the
    /// stroke. The hex stays constant; the envelope is an opacity
    /// curve. `bloomCenterAlpha` is the resting center alpha;
    /// `bloomBreathPeak` is the max the glide animation breathes up
    /// to. `bloomMidAlpha` is the alpha at the 50% color-stop.
    public let bloomColor: NSColor
    public let bloomCenterAlpha: CGFloat
    public let bloomMidAlpha: CGFloat
    public let bloomBreathPeak: CGFloat

    public init(
        containerSize: CGFloat,
        shapeSize: CGFloat,
        strokeGradientStops: [AgentCursorGradientStop],
        strokeGradientAngleDegrees: CGFloat,
        strokeWidth: CGFloat,
        highlightStrokeWidth: CGFloat,
        bloomColor: NSColor,
        bloomCenterAlpha: CGFloat,
        bloomMidAlpha: CGFloat,
        bloomBreathPeak: CGFloat
    ) {
        self.containerSize = containerSize
        self.shapeSize = shapeSize
        self.strokeGradientStops = strokeGradientStops
        self.strokeGradientAngleDegrees = strokeGradientAngleDegrees
        self.strokeWidth = strokeWidth
        self.highlightStrokeWidth = highlightStrokeWidth
        self.bloomColor = bloomColor
        self.bloomCenterAlpha = bloomCenterAlpha
        self.bloomMidAlpha = bloomMidAlpha
        self.bloomBreathPeak = bloomBreathPeak
    }

    /// The single locked-in style. Values per the design spec. Edit
    /// these to retint / resize the pointer — no other call sites
    /// should be reading them directly.
    public static let `default` = AgentCursorStyle(
        containerSize: 60,
        // SVG content fills ~18/24 of the shape frame (has built-in
        // padding), so the effective drawn size is 75% of `shapeSize`.
        // 22pt gives a visible cursor around 16-17pt — comparable to the
        // macOS system cursor.
        shapeSize: 22,
        // cua-driver heritage gradient: ice-blue tip → cyan body → mint
        // tail. Axial 135° (from upper-left to lower-right) traces the
        // cursor's own tip-to-tail axis, so the tip reads brightest.
        strokeGradientStops: [
            AgentCursorGradientStop(
                color: NSColor(red: 0xDB / 255, green: 0xEE / 255, blue: 0xFF / 255, alpha: 1),
                location: 0.0
            ),
            AgentCursorGradientStop(
                color: NSColor(red: 0x5E / 255, green: 0xC0 / 255, blue: 0xE8 / 255, alpha: 1),
                location: 0.53
            ),
            AgentCursorGradientStop(
                color: NSColor(red: 0x54 / 255, green: 0xCD / 255, blue: 0xA0 / 255, alpha: 1),
                location: 1.0
            ),
        ],
        strokeGradientAngleDegrees: 135,
        // 2pt white outline wraps the gradient-filled shape. Highlight
        // stroke field is retained in the style struct for back-compat
        // but not used by the current renderer (the layer tree has
        // gradient-fill + white-border, no separate highlight stroke).
        strokeWidth: 2,
        highlightStrokeWidth: 0.5,
        // Bloom matches the gradient's mid stop (cyan) so the halo reads
        // as the cursor "exhaling color" rather than an unrelated hue.
        bloomColor: NSColor(red: 0x5E / 255, green: 0xC0 / 255, blue: 0xE8 / 255, alpha: 1),
        bloomCenterAlpha: 0.55,
        bloomMidAlpha: 0.15,
        bloomBreathPeak: 0.75
    )
}

/// The agent cursor overlay — a purely visual floating arrow that
/// shows where the agent is "looking" while it works. It does NOT
/// deliver input events; the driver's real clicks continue to run
/// through the AX-element_index path (invisible AX RPC). This overlay
/// is a trust signal: the user sees what the agent is targeting.
///
/// ## Lifecycle
///
/// The overlay is lazy — the window + view are created on first
/// `show()` call and retained for the lifetime of the process. `hide()`
/// orders the window off-screen but doesn't tear down the view tree;
/// the next `show()` is effectively instant.
///
/// ## Threading
///
/// Everything here is `@MainActor`-isolated because AppKit requires it.
/// Call from a `Task { @MainActor in … }` block if you're coming from
/// an async non-main context.
///
/// ## Run-loop prerequisite
///
/// AppKit drawing + CA animations need a live main-thread run loop
/// pumping events. The driver's default stdio-MCP entry point does NOT
/// currently bootstrap `NSApplication.shared.run()` — wire-up for that
/// lives outside this module (a later commit). Until that wire-up is
/// in place, `show()` still succeeds but the cursor won't draw or
/// animate. That's fine for the first commit: the types compile and
/// unit tests can exercise the math without a running screen.
@MainActor
public final class AgentCursor {
    public static let shared = AgentCursor()

    /// Master toggle. When false, `show`/`animate` are no-ops and no
    /// window is created. Defaults to `true` — the driver is
    /// primarily used for user-visible demos where the trust signal
    /// matters more than absolute-silent automation. Headless / CI
    /// callers can opt out with `set_agent_cursor_enabled '{"enabled":false}'`.
    public private(set) var isEnabled: Bool = true

    /// Session-level motion defaults used by `animateAndWait` and
    /// the single-argument overload of `animate`. Tunable at runtime
    /// via the `set_agent_cursor_motion` MCP tool.
    public var defaultMotionOptions: CursorMotionPath.Options = .default

    /// How long each cursor glide takes. Used as the default
    /// `duration` for `animateAndWait`/`animate` when the caller
    /// doesn't pass one. 0.75s reads cleanly in demos even over
    /// short inter-button paths (~50pt); crank it up further for
    /// screen recordings via `set_agent_cursor_motion`.
    public var glideDurationSeconds: CFTimeInterval = 0.75

    /// Post-ripple pause before the tool returns, letting the cursor
    /// visibly rest on the target so a sequence of clicks reads as
    /// deliberate human pacing rather than a blur. Only applied when
    /// the overlay is enabled (invisible automation pays no cost).
    /// 0.4s pairs well with the default glide.
    public var dwellAfterClickSeconds: CFTimeInterval = 0.4

    /// How long the overlay lingers after the last pointer action
    /// before it auto-hides. Each `animateAndWait` / `finishClick`
    /// resets this timer, so a burst of back-to-back clicks keeps the
    /// cursor visible throughout; then it slides off after the driver
    /// has been idle this long. 3s leaves comfortable headroom for a
    /// follow-up action to arrive without the overlay popping in and
    /// out, while still being short enough that the cursor is gone
    /// before the user starts wondering if the agent is still running.
    public var idleHideDelay: TimeInterval = 3.0

    private var overlay: AgentCursorOverlayWindow?
    private var view: AgentCursorView?
    private var idleHideTask: Task<Void, Never>?

    /// CGWindowID of the target window the overlay is currently
    /// z-pinned above. Cached so consecutive clicks on the same
    /// target skip redundant `NSWindow.order(_:relativeTo:)` calls,
    /// which would otherwise cause a one-frame flash as the window
    /// server re-composites.
    private var pinnedWindowId: Int?

    /// pid of the app the overlay is currently z-pinned above.
    /// The workspace observer re-runs `pinAbove` for this pid
    /// whenever any app activation notification fires, which
    /// catches the async "raise" that macOS processes a few
    /// frames after an AX click returns.
    private var pinnedPid: pid_t?

    /// Observer token for `NSWorkspace.didActivateApplicationNotification`.
    /// Registered lazily on first `pinAbove` and invalidated when
    /// the overlay is hidden or the cursor disabled.
    private var activationObserver: NSObjectProtocol?

    /// Short-lived task that re-runs `pinAbove` a few times after
    /// each click to catch the async window-level raise that the
    /// target does without the app becoming frontmost (so the
    /// activation observer misses it). Cancelled and respawned on
    /// every `pinAbove` call.
    private var defensiveRepinTask: Task<Void, Never>?

    /// Count of consecutive `reapplyPinAbove` ticks that couldn't
    /// find the pinned pid's on-screen window. Used to tolerate
    /// single-frame misses during target redraw / raise animations
    /// — hiding the overlay on the first miss caused a visible
    /// "cursor disappears for ~1s during click" because mid-raise
    /// window enumerations transiently return no match. `orderOut`
    /// only fires after ≥2 consecutive misses.
    private var missedPinCount: Int = 0

    private init() {}

    /// Enable or disable the cursor. Disabling immediately hides the
    /// overlay and cancels any in-flight animations and idle timers.
    ///
    /// Disable fully tears down the window + view stored properties
    /// (via `close()`, then nils them) so the next `show()` rebuilds
    /// a fresh overlay via `ensureWindow()`. An earlier version only
    /// called `orderOut` and kept the stored references; on a subsequent
    /// enable the retained NSWindow would no longer reliably re-register
    /// with the window server via `orderFront(nil)` — `list_windows`
    /// would return zero windows for the daemon pid and the cursor
    /// would stay invisible through every click. Mirroring the
    /// daemon-restart rebuild path (fresh window) is what keeps the
    /// enable-after-disable path working.
    public func setEnabled(_ enabled: Bool) {
        guard isEnabled != enabled else { return }
        isEnabled = enabled
        if !enabled {
            cancelIdleHide()
            hide()
            view?.cursorLayer.removeAllAnimations()
            tearDownActivationObserver()
            pinnedPid = nil
            // Drop the NSWindow + content view so a later enable +
            // show() rebuilds from scratch. See docstring above.
            overlay?.close()
            overlay = nil
            view = nil
        }
    }

    /// Apply a persisted ``AgentCursorConfig`` to the live singleton.
    /// Used at daemon boot so `AgentCursor.shared` starts in the state
    /// the user last wrote, rather than the compiled-in defaults. Any
    /// future knobs added to `AgentCursorConfig` should propagate here
    /// so the boot-time snapshot is a single source of truth.
    public func apply(config: AgentCursorConfig) {
        setEnabled(config.enabled)
        defaultMotionOptions = CursorMotionPath.Options(
            startHandle: CGFloat(config.motion.startHandle),
            endHandle: CGFloat(config.motion.endHandle),
            arcSize: CGFloat(config.motion.arcSize),
            arcFlow: CGFloat(config.motion.arcFlow),
            spring: CGFloat(config.motion.spring)
        )
    }

    /// Show the overlay window. Idempotent — successive calls are
    /// cheap no-ops once the window is already visible. Creates the
    /// window + content view on first call. No-op when disabled.
    ///
    /// Uses `orderFrontRegardless()` rather than `orderFront(nil)`:
    /// the daemon runs under `.accessory` activation policy, and a
    /// freshly-allocated `.floating`-level, clear-background,
    /// borderless NSWindow from an accessory-policy app
    /// `orderFront(nil)`'d doesn't become key-window-eligible →
    /// WindowServer marks it `kCGWindowIsOnscreen = false` even
    /// though we ordered it. Result: the cyan overlay is present in
    /// the window list but not composited, so SCStream capture and
    /// visual rendering both miss it. `orderFrontRegardless()`
    /// forces the on-screen transition without requiring the app
    /// to activate — exactly the semantics we want (backgrounded
    /// overlay that is always visible but never key).
    public func show() {
        guard isEnabled else { return }
        let win = ensureWindow()
        if !win.isVisible {
            win.orderFrontRegardless()
        }
    }

    /// Pin the overlay just above the given pid's frontmost on-screen
    /// window. Keeps the overlay at `.floating` (the init default)
    /// and orders it above the target window so consecutive clicks on
    /// the same target skip redundant re-orders. See
    /// `reapplyPinAbove()` for the rationale on staying at `.floating`
    /// instead of demoting to `.normal` — short version: `.normal`
    /// lets Electron / Chromium targets transiently push themselves
    /// above the cursor mid-click, making the overlay read as "blinks
    /// out for a frame" on every click.
    ///
    /// When the target has no on-screen window (hidden launch still
    /// pending, offscreen window, etc.) the overlay is ordered out
    /// after ≥2 consecutive missed ticks — rather than floating over
    /// unrelated apps the user happens to have frontmost.
    public func pinAbove(pid: pid_t) {
        guard isEnabled else { return }
        pinnedPid = pid
        missedPinCount = 0  // fresh pin — any earlier miss streak is stale
        ensureActivationObserver()
        reapplyPinAbove()
        scheduleDefensiveRepin()
    }

    /// Re-run `pinAbove` a few times over the next ~1200ms to catch
    /// the async window-level raise macOS sometimes does a few
    /// frames after an AX click — when the target's *window* rises
    /// in z-order but the *app* doesn't become frontmost, so
    /// `didActivateApplicationNotification` never fires. Each call
    /// is idempotent when the overlay is already correctly pinned;
    /// cheap enough to run on a short schedule.
    ///
    /// Coverage must span the full click lifecycle — `playClickPress`
    /// runs for 650ms and `finishClick`'s dwell adds another
    /// ~250ms. An earlier 700ms schedule let late-arriving target
    /// raises (Electron / redraw-heavy apps in particular) land
    /// after the last tick, stranding the overlay under the target
    /// for the rest of the ripple. The current schedule tails out
    /// to 1200ms with buffer, so ticks keep firing through the
    /// ripple + dwell even for slower targets.
    private func scheduleDefensiveRepin() {
        defensiveRepinTask?.cancel()
        defensiveRepinTask = Task { @MainActor [weak self] in
            for delayMs in [60, 180, 360, 600, 900, 1200] {
                try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
                guard let self, !Task.isCancelled else { return }
                self.reapplyPinAbove()
            }
        }
    }

    /// Re-run the pin for the most recent `pinnedPid`. Called by
    /// `pinAbove` and by the workspace activation observer.
    private func reapplyPinAbove() {
        guard isEnabled, let pid = pinnedPid else { return }
        let win = ensureWindow()

        // Find target's frontmost on-screen "normal-layer" window
        // (layer == 0). Dock, menu bar, and shields show up at higher
        // layers and aren't what the caller is clicking.
        let targetWindow = WindowEnumerator.visibleWindows()
            .filter { $0.pid == pid && $0.layer == 0 && $0.isOnScreen }
            .max(by: { $0.zIndex < $1.zIndex })

        guard let targetWindow else {
            // Target has no on-screen window — it's minimized, hidden,
            // or on another Space. Drop the overlay entirely rather
            // than floating it above other apps: there's nothing to
            // pin above, and showing a stranded cursor over the user's
            // actual frontmost app is worse than nothing.
            //
            // BUT — a single missed tick is usually just a mid-raise
            // frame where `visibleWindows()` transiently returns no
            // match. Hiding on the first miss caused the overlay to
            // vanish for ~1s during every click. Require ≥2
            // consecutive misses before hiding; the next scheduled
            // repin tick (60–300ms later) will catch the window
            // once it's back on screen and reset the counter.
            missedPinCount += 1
            if missedPinCount >= 2 {
                if win.isVisible { win.orderOut(nil) }
                pinnedWindowId = nil
            }
            return
        }
        missedPinCount = 0
        // Keep the overlay at its initial `.floating` level — above
        // ordinary `.normal` app windows without competing for z-order
        // against them. Previously this demoted the overlay to
        // `.normal` and re-ordered it just above the target so
        // unrelated apps stacked over the target would occlude the
        // cursor (aesthetic nicety). But at `.normal`, any redraw /
        // re-raise on the target — especially for Electron / Chromium
        // apps that re-stack their own window on AX-dispatched clicks
        // — can transiently push the target above the overlay. Ripple
        // animation then plays behind the app and the cursor reads
        // as "disappears for a second" on every click. At `.floating`,
        // the window server guarantees ordering against `.normal`, so
        // the cursor stays visible through the click regardless of
        // what the target does to its own z-stack.
        //
        // `order(.above, relativeTo:)` is still worth running: it
        // keeps the overlay above other `.floating` windows (ours or
        // the system's) that might otherwise appear over it.
        win.order(.above, relativeTo: targetWindow.id)
        pinnedWindowId = targetWindow.id
    }

    /// Lazily register a `didActivateApplicationNotification`
    /// observer that re-pins whenever any app activates. AX-
    /// dispatched clicks raise the target asynchronously — often
    /// a few frames after `performAction` returns — so a single
    /// post-click `pinAbove` can fire before the raise lands and
    /// leaves the overlay stranded underneath. The activation
    /// notification is the ground-truth signal for "some window
    /// just changed z-order"; re-pinning on every one of them
    /// closes the race at essentially zero cost (observer only
    /// fires on user- and system-level activation events).
    private func ensureActivationObserver() {
        guard activationObserver == nil else { return }
        activationObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            // `queue: .main` hops to the main thread, but that's
            // NOT the same as main-actor isolation in Swift
            // concurrency — hop explicitly so we can call the
            // actor-isolated reapplyPinAbove().
            Task { @MainActor in
                self?.reapplyPinAbove()
            }
        }
    }

    private func tearDownActivationObserver() {
        if let obs = activationObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(obs)
            activationObserver = nil
        }
        defensiveRepinTask?.cancel()
        defensiveRepinTask = nil
    }

    /// Hide the overlay window. No-op if not shown. Keeps the window
    /// retained so the next `show()` is instant.
    ///
    /// Also clears the pin state and cancels the defensive repin
    /// task. Without this cleanup, subsequent
    /// `didActivateApplicationNotification` events (or any still-
    /// queued defensive repin ticks) would call `reapplyPinAbove`,
    /// which re-orders the overlay window into the z-stack — visibly
    /// resurrecting the cursor the idle-hide timer just removed.
    public func hide() {
        overlay?.orderOut(nil)
        pinnedWindowId = nil
        pinnedPid = nil
        missedPinCount = 0
        defensiveRepinTask?.cancel()
        defensiveRepinTask = nil
    }

    /// Move the cursor immediately to a screen-point coordinate. No
    /// animation. Use `animate(to:duration:)` for smooth motion.
    /// Coordinates are screen points (top-left origin), matching what
    /// `AXUIElement`'s `AXPosition` attribute returns.
    public func setPosition(_ point: CGPoint) {
        ensureView().setPosition(point)
    }

    /// Animate the cursor to `point`, then suspend until the glide is
    /// complete. Use this from tool-invocation sites so the AX action
    /// fires after the user has seen the cursor arrive at the target.
    ///
    /// No-op (returns immediately) when disabled, so call sites don't
    /// need to branch on `isEnabled` — they just `await` unconditionally.
    public func animateAndWait(
        to point: CGPoint,
        duration: CFTimeInterval? = nil,
        options: CursorMotionPath.Options? = nil
    ) async {
        guard isEnabled else { return }
        let effectiveOptions = options ?? defaultMotionOptions
        let duration = duration ?? glideDurationSeconds
        cancelIdleHide()  // incoming activity — defer auto-hide
        show()  // ensure the overlay is visible; no-op if already shown
        animate(to: point, duration: duration, options: effectiveOptions)
        // Sleep the caller for the glide duration. Rotation settle
        // continues after `duration` but the cursor has already
        // arrived at the target by then; the caller is free to
        // dispatch the AX action.
        try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
    }

    /// Animate the cursor to `point` over `duration` seconds along a
    /// cubic-Bezier arc. Pass `options: .default` for tuned defaults
    /// or override the 5 knobs for custom motion.
    ///
    /// On arrival the rotation is settled via a spring animation
    /// driven by the `spring` knob, so the cursor lands at its
    /// resting tilt (-35°) with a damped overshoot.
    ///
    /// No-op when disabled.
    public func animate(
        to point: CGPoint,
        duration: CFTimeInterval? = nil,
        options: CursorMotionPath.Options? = nil
    ) {
        guard isEnabled else { return }
        let options = options ?? defaultMotionOptions
        let duration = duration ?? glideDurationSeconds
        let view = ensureView()
        let from = view.cursorLayer.position
        let path = CursorMotionPath(from: from, to: point, options: options)

        // Main glide along the Bezier.
        let glide = path.positionAnimation(duration: duration)
        view.cursorLayer.add(glide, forKey: "glide")
        view.setPosition(point)

        // Bloom breath — while the cursor is gliding, pulse the
        // radial bloom's center alpha up and back so the halo feels
        // "alive" during agent action and subtle when at rest. The
        // envelope matches the glide duration exactly so the halo
        // resolves to its resting alpha as the cursor lands.
        playBloomBreath(bloomLayer: view.bloomLayer, duration: duration)

        // Note: no rotation. Previous builds rotated the cursor to
        // match the motion tangent on arrival; the SVG pointer path
        // already has the tip at upper-left and the ring-ripple on
        // click-landing carries the arrival-beat visual.
    }

    /// Pulse the bloom's center-stop alpha up to
    /// `AgentCursorStyle.bloomBreathPeak` and back over `duration`,
    /// giving the halo a "breath" during a glide. Animates the
    /// gradient layer's `colors` array (three NSColor stops with
    /// matching alphas) so only the center stop's alpha varies — the
    /// falloff shape stays constant. `isRemovedOnCompletion = true`
    /// restores the static colors at the end.
    private func playBloomBreath(bloomLayer: CAGradientLayer, duration: CFTimeInterval) {
        let style = AgentCursorStyle.default
        let bloom = style.bloomColor
        let mid = bloom.withAlphaComponent(style.bloomMidAlpha).cgColor
        let edge = bloom.withAlphaComponent(0.0).cgColor
        let rest: [CGColor] = [
            bloom.withAlphaComponent(style.bloomCenterAlpha).cgColor, mid, edge,
        ]
        let peak: [CGColor] = [
            bloom.withAlphaComponent(style.bloomBreathPeak).cgColor, mid, edge,
        ]
        let anim = CAKeyframeAnimation(keyPath: "colors")
        anim.values = [rest, peak, rest]
        anim.keyTimes = [0.0, 0.5, 1.0]
        anim.duration = duration
        anim.timingFunctions = [
            CAMediaTimingFunction(name: .easeInEaseOut),
            CAMediaTimingFunction(name: .easeInEaseOut),
        ]
        anim.isRemovedOnCompletion = true
        bloomLayer.add(anim, forKey: "bloomBreath")
    }

    /// Emit a ring-ripple centered on the cursor's tip — a circle
    /// that starts small and opaque, expands outward while fading,
    /// and removes itself when done. Fires right after the AX action
    /// so the viewer sees "something landed here" without the cursor
    /// itself moving or rotating.
    ///
    /// Implemented as a short-lived `CAShapeLayer` added as a child
    /// of the cursor container. Because the container's anchor is at
    /// the tip, adding the ripple centered on the anchor auto-aligns
    /// it with the click point. Layer is removed after the animation
    /// so repeated clicks don't accumulate dead sublayers.
    ///
    /// No-op when disabled. Caller awaits the full duration so the
    /// ripple visibly completes before the dwell starts.
    public func playClickPress(duration: CFTimeInterval = 0.65) async {
        guard isEnabled, let cursorLayer = view?.cursorLayer else { return }

        // Center all rings on the cursor's tip. The container's
        // anchor encodes the tip in normalized (0–1) coords; scale
        // to container-local to place the rings.
        let anchor = cursorLayer.anchorPoint
        let size = cursorLayer.bounds.size
        let tip = CGPoint(x: anchor.x * size.width, y: anchor.y * size.height)

        // Three concentric rings expanding outward together. Innermost
        // is the brightest + smallest; each successive ring is larger
        // and more transparent — a ripple "stack" that reads as
        // radiating presence rather than a single flashbulb. Each
        // ring still uses the same scale curve so they stay
        // proportional as they grow.
        struct Ring {
            let startDiameter: CGFloat
            let peakAlpha: Double
        }
        let rings: [Ring] = [
            Ring(startDiameter: 6,  peakAlpha: 0.30),  // inner
            Ring(startDiameter: 10, peakAlpha: 0.12),  // outer — ghost
        ]
        let endScale: CGFloat = 1.8

        // Collect the layers so we can tear them down cleanly below.
        var rippleLayers: [CAShapeLayer] = []

        for ring in rings {
            let layer = CAShapeLayer()
            let rect = CGRect(
                origin: .zero,
                size: CGSize(width: ring.startDiameter, height: ring.startDiameter)
            )
            layer.path = CGPath(ellipseIn: rect, transform: nil)
            layer.fillColor = NSColor.clear.cgColor
            layer.strokeColor = NSColor.white.withAlphaComponent(ring.peakAlpha).cgColor
            layer.lineWidth = 0.8
            layer.frame = CGRect(
                x: tip.x - ring.startDiameter / 2,
                y: tip.y - ring.startDiameter / 2,
                width: ring.startDiameter,
                height: ring.startDiameter
            )
            layer.opacity = 0  // starts invisible; opacity keyframe ramps in
            cursorLayer.addSublayer(layer)
            rippleLayers.append(layer)

            // Scale is identical across rings so they stay concentric as
            // they grow. Ease-out curve punches outward and settles.
            let scaleAnim = CABasicAnimation(keyPath: "transform.scale")
            scaleAnim.fromValue = 0.7
            scaleAnim.toValue = endScale
            scaleAnim.timingFunction = CAMediaTimingFunction(
                controlPoints: 0.16, 1, 0.3, 1)
            scaleAnim.duration = duration

            // Peak is 1.0 here — the real peak alpha is baked into
            // `strokeColor`'s alpha. This keyframe just ramps the ring
            // in briefly then fades over a long tail.
            let opacityAnim = CAKeyframeAnimation(keyPath: "opacity")
            opacityAnim.values = [0.0, 1.0, 0.0]
            opacityAnim.keyTimes = [0.0, 0.1, 1.0]
            opacityAnim.duration = duration

            let group = CAAnimationGroup()
            group.animations = [scaleAnim, opacityAnim]
            group.duration = duration
            group.isRemovedOnCompletion = true
            layer.add(group, forKey: "ripple")
        }

        try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
        for layer in rippleLayers {
            layer.removeFromSuperlayer()
        }
    }

    /// Mark the "click landed" moment — pauses the caller for the
    /// configured dwell so the cursor visibly rests on the target
    /// before the next flight, then arms the idle-hide timer. No
    /// visual effect; the overlay is just the triangle. Call after
    /// the AX action + post-AX re-pin + press animation. The `pid`
    /// argument is unused here but kept so callers pass the target
    /// context at the click-lifecycle boundary.
    ///
    /// No-op when disabled.
    public func finishClick(pid: pid_t) async {
        _ = pid  // reserved for future per-pid dwell / hide policy
        guard isEnabled else { return }

        // Human-pacing dwell: pause the caller (and thus the next AX
        // action) so the cursor visibly rests on the target before the
        // next flight starts. Keeps back-to-back clicks from reading
        // as a blur.
        if dwellAfterClickSeconds > 0 {
            try? await Task.sleep(
                nanoseconds: UInt64(dwellAfterClickSeconds * 1_000_000_000))
        }

        // "Click landed" is the natural moment to arm the idle timer
        // so the overlay auto-hides if no further clicks arrive within
        // `idleHideDelay`. Any subsequent `animateAndWait` cancels
        // this timer and reschedules, so consecutive clicks keep the
        // overlay visible throughout the burst.
        scheduleIdleHide()
    }

    /// For tests + spike code: tear down the window so the next
    /// `show()` rebuilds it from scratch. Not part of the public
    /// tool-surface contract.
    public func resetForTesting() {
        cancelIdleHide()
        overlay?.orderOut(nil)
        overlay = nil
        view = nil
    }

    // MARK: - Private

    /// Arm (or re-arm) the idle auto-hide timer. Cancels any previously
    /// scheduled hide so the most recent activity wins.
    private func scheduleIdleHide() {
        cancelIdleHide()
        let delay = idleHideDelay
        idleHideTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await MainActor.run {
                guard let self else { return }
                // A late-arriving click may have flipped isEnabled off
                // or swapped out the overlay; both branches are safe.
                self.hide()
                self.idleHideTask = nil
            }
        }
    }

    private func cancelIdleHide() {
        idleHideTask?.cancel()
        idleHideTask = nil
    }

    private func ensureWindow() -> AgentCursorOverlayWindow {
        if let overlay { return overlay }
        let win = AgentCursorOverlayWindow()
        let view = AgentCursorView(frame: win.frame)
        win.contentView = view
        self.overlay = win
        self.view = view
        return win
    }

    private func ensureView() -> AgentCursorView {
        _ = ensureWindow()
        return view!  // set by ensureWindow's side effect
    }
}
