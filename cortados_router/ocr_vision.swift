// Local OCR for router image conversion: macOS Vision text recognition, no network, no model call.
// Usage: ocr_vision <image path> | --warm   →   one JSON object on stdout:
//   {"engine":"apple-vision","lines":[{"text":"…","confidence":0.0–1.0}, …]}
// Exit 2 when the image cannot be read; the caller then falls back to another engine or to a vision model.
import Foundation
import Vision

struct Line: Codable { let text: String; let confidence: Float }
struct Output: Codable { let engine: String; let lines: [Line] }

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: ocr_vision <image>\n".data(using: .utf8)!)
    exit(64)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// --warm: recognise a blank image so Vision loads its model (~55 s on a fresh binary or after a reboot, then ~0.2 s).
let handler: VNImageRequestHandler
if CommandLine.arguments[1] == "--warm" {
    let space = CGColorSpaceCreateDeviceGray()
    let ctx = CGContext(data: nil, width: 64, height: 64, bitsPerComponent: 8, bytesPerRow: 64, space: space, bitmapInfo: 0)!
    handler = VNImageRequestHandler(cgImage: ctx.makeImage()!, options: [:])
} else {
    handler = VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1]), options: [:])
}
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("unreadable image\n".data(using: .utf8)!)
    exit(2)
}
let lines = (request.results ?? []).compactMap { obs -> Line? in
    guard let best = obs.topCandidates(1).first else { return nil }
    return Line(text: best.string, confidence: best.confidence)
}
let data = try JSONEncoder().encode(Output(engine: "apple-vision", lines: lines))
FileHandle.standardOutput.write(data)
