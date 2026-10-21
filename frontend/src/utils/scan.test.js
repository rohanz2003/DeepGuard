import { describe, expect, it } from "vitest";
import { normalizeScan } from "./scan";

describe("scan normalization privacy boundary", () => {
  it("keeps playback metadata but strips raw audio and transcript content", () => {
    const normalized = normalizeScan({
      id: "private-scan",
      transcript: "private words",
      raw_transcript: "private words",
      audio: new Blob(["RIFF"]),
      audio_bytes: new Uint8Array([82, 73, 70, 70]),
      audio_retained: true,
      audio_playback_endpoint: "/scans/private-scan/audio",
    });

    expect(normalized).toMatchObject({
      id: "private-scan",
      audio_retained: true,
      audio_playback_endpoint: "/scans/private-scan/audio",
    });
    expect(normalized).not.toHaveProperty("transcript");
    expect(normalized).not.toHaveProperty("raw_transcript");
    expect(normalized).not.toHaveProperty("audio");
    expect(normalized).not.toHaveProperty("audio_bytes");
  });
});
