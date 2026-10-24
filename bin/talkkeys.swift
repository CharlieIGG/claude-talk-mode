// Keyboard keys for talk mode, mainly for the laptop speakers (no earbud double
// tap there, and talking over Claude is off because of echo protection):
//   microphone key (F5, key code 176)  pause / resume listening
//   moon key       (F6, key code 178)  stop Claude's voice
// start.sh builds this with swiftc into state/talkkeys and runs it while talk mode
// is on; it exits by itself within 2 s of runtime/active disappearing. Only these
// two keys are swallowed, so dictation and Do Not Disturb work normally otherwise.
import Cocoa

let root = URL(fileURLWithPath: CommandLine.arguments[0])
    .resolvingSymlinksInPath().deletingLastPathComponent().deletingLastPathComponent()
let active = root.appendingPathComponent("runtime/active").path
let talkctl = root.appendingPathComponent("bin/talkctl.py").path
let actions: [Int64: String] = [176: "mute", 178: "quiet"]

func log(_ msg: String) {
    FileHandle.standardError.write("[keys] \(msg)\n".data(using: .utf8)!)
}

func run(_ action: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    p.arguments = ["-I", talkctl, "key", action]
    try? p.run()
}

var tap: CFMachPort?
let callback: CGEventTapCallBack = { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let tap = tap { CGEvent.tapEnable(tap: tap, enable: true) }
        return Unmanaged.passUnretained(event)
    }
    let code = event.getIntegerValueField(.keyboardEventKeycode)
    guard let action = actions[code] else { return Unmanaged.passUnretained(event) }
    if type == .keyDown && event.getIntegerValueField(.keyboardEventAutorepeat) == 0 {
        log(action)
        run(action)
    }
    return nil                                         // swallow both key down and key up
}

let mask: CGEventMask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue)
tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
                        options: .defaultTap, eventsOfInterest: mask,
                        callback: callback, userInfo: nil)
guard let tap = tap else {
    log("no keyboard access; allow your terminal app in System Settings > Privacy & Security > Accessibility")
    exit(2)
}
CFRunLoopAddSource(CFRunLoopGetCurrent(), CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0), .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)
log("ready: microphone key = pause listening, moon key = stop voice")

Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in
    if !FileManager.default.fileExists(atPath: active) { exit(0) }
}
CFRunLoopRun()
