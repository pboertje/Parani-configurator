// Terminal to a Parani-SD1000 over Bluetooth, without /dev/cu.* (the macOS serial driver
// only connects the first time after pairing; direct RFCOMM always works).
// Build:    swiftc -o parani-term parani-term.swift
// Usage:    ./parani-term [address]      default 00-01-95-12-34-56, RFCOMM channel 1
// Quit:     Ctrl-]
import Foundation
import IOBluetooth

setvbuf(stdout, nil, _IONBF, 0)
let addr = CommandLine.arguments.dropFirst().first ?? "00-01-95-12-34-56"
guard let dev = IOBluetoothDevice(addressString: addr) else { print("invalid address \(addr)"); exit(2) }

// Put the terminal in raw mode (only if stdin is a terminal)
var saved = termios()
let isTTY = isatty(STDIN_FILENO) == 1
func restore() { if isTTY { tcsetattr(STDIN_FILENO, TCSANOW, &saved) } }
if isTTY {
  tcgetattr(STDIN_FILENO, &saved)
  var raw = saved; cfmakeraw(&raw); tcsetattr(STDIN_FILENO, TCSANOW, &raw)
}
func say(_ s: String) { FileHandle.standardError.write(("\r\n[parani] " + s + "\r\n").data(using: .utf8)!) }
func quit(_ code: Int32) -> Never { restore(); exit(code) }

var channel: IOBluetoothRFCOMMChannel?
class Delegate: NSObject, IOBluetoothRFCOMMChannelDelegate {
  func rfcommChannelOpenComplete(_ ch: IOBluetoothRFCOMMChannel!, status: IOReturn) {
    if status != 0 { say("connection failed (status \(status))"); quit(1) }
    say("connected to \(ch.getDevice()?.name ?? "Parani"); Ctrl-] to quit")
    startStdin(ch)
  }
  func rfcommChannelData(_ ch: IOBluetoothRFCOMMChannel!, data: UnsafeMutableRawPointer!, length: Int) {
    FileHandle.standardOutput.write(Data(bytes: data, count: length))
  }
  func rfcommChannelClosed(_ ch: IOBluetoothRFCOMMChannel!) { say("connection closed"); quit(0) }
}

func startStdin(_ ch: IOBluetoothRFCOMMChannel) {
  let mtu = Int(ch.getMTU())
  FileHandle.standardInput.readabilityHandler = { h in
    var bytes = [UInt8](h.availableData)
    if bytes.isEmpty {                      // EOF (stdin not a terminal): wait briefly for a response
      h.readabilityHandler = nil
      DispatchQueue.main.asyncAfter(deadline: .now() + 3) { let d = ch.getDevice(); ch.close(); d?.closeConnection(); quit(0) }
      return
    }
    if let i = bytes.firstIndex(of: 0x1d) {  // Ctrl-]
      bytes = Array(bytes[..<i])
      DispatchQueue.main.async { send(ch, bytes, mtu); let d = ch.getDevice(); ch.close(); d?.closeConnection(); say("stopped"); quit(0) }
      return
    }
    DispatchQueue.main.async { send(ch, bytes, mtu) }
  }
}
func send(_ ch: IOBluetoothRFCOMMChannel, _ bytes: [UInt8], _ mtu: Int) {
  var off = 0
  while off < bytes.count {
    var chunk = Array(bytes[off..<min(off + mtu, bytes.count)])
    ch.writeSync(&chunk, length: UInt16(chunk.count)); off += chunk.count
  }
}

let delegate = Delegate()
signal(SIGINT, SIG_IGN)
say("connecting to \(addr)…")
let r = dev.openRFCOMMChannelAsync(&channel, withChannelID: 1, delegate: delegate)
if r != 0 { say("connection failed (\(r)); is the Parani powered on and paired?"); quit(1) }
DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
  if channel?.isOpen() != true { say("no connection within 20 s"); quit(1) }
}
RunLoop.main.run()
