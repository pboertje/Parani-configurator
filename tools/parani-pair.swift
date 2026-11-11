// Pair a Parani-SD1000 with the Mac over Bluetooth (System Settings refuses it
// because it reports Class of Device 0x000000).
// Build:    swiftc -o parani-pair parani-pair.swift
// Usage:    ./parani-pair <pin> [address]       pair (default 00-01-95-12-34-56)
//           ./parani-pair --remove [address]    remove the pairing from the Mac
import Foundation
import IOBluetooth
setvbuf(stdout, nil, _IONBF, 0)

var args = Array(CommandLine.arguments.dropFirst())
guard !args.isEmpty else { print("usage: parani-pair <pin>|--remove [address]"); exit(2) }
let first = args.removeFirst()
let addr = args.first ?? "00-01-95-12-34-56"
guard let dev = IOBluetoothDevice(addressString: addr) else { print("invalid address \(addr)"); exit(2) }

if first == "--remove" {
  if dev.isPaired() { dev.perform(Selector(("remove"))) }
  RunLoop.main.run(until: Date().addingTimeInterval(1))
  print("paired:", dev.isPaired()); exit(dev.isPaired() ? 1 : 0)
}

let pinCode = first
guard (1...16).contains(pinCode.utf8.count) else { print("PIN must be 1-16 characters"); exit(2) }
var done = false, result: IOReturn = -1
class P: NSObject, IOBluetoothDevicePairDelegate {
  func devicePairingPINCodeRequest(_ s: Any!) {
    print("PIN requested, sending")
    var pin = BluetoothPINCode()
    withUnsafeMutableBytes(of: &pin) { b in for (i, c) in pinCode.utf8.enumerated() { b[i] = c } }
    (s as! IOBluetoothDevicePair).replyPINCode(pinCode.utf8.count, pinCode: &pin)
  }
  func devicePairingUserConfirmationRequest(_ s: Any!, numericValue: BluetoothNumericValue) {
    (s as! IOBluetoothDevicePair).replyUserConfirmation(true)
  }
  func devicePairingFinished(_ s: Any!, error: IOReturn) { if !done { result = error; done = true } }
}
let p = P(); let pair = IOBluetoothDevicePair(device: dev)!; pair.delegate = p
print("pairing with \(addr)…"); _ = pair.start()
let end = Date().addingTimeInterval(40)
while !done && Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.5)) }
print(dev.isPaired() && result == 0 ? "paired: /dev/cu.\(dev.name ?? "?") (it may take a moment to appear)"
                                    : "failed (code \(result)); wrong PIN or Parani not in MODE3?")
exit(dev.isPaired() && result == 0 ? 0 : 1)
