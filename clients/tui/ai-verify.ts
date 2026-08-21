// Verify the LIVE AI Keeper: join the room and send plain-language input,
// then watch for a model-generated narrative (NOT the scripted demo reply).
// Usage: bun run ai-verify.ts <ticket> <key>
import { IrohClient } from "./src/irohClient"
import type { ServerFrame } from "loreweaver-protocol"

const [ticket, key] = process.argv.slice(2)
if (!ticket || !key) {
  console.error("usage: bun run ai-verify.ts <ticket> <key>")
  process.exit(2)
}

const frames: ServerFrame[] = []
const client = new IrohClient({ reconnect: false })
client.onMessage((f) => frames.push(f))

console.log("dialing...")
await client.connect(ticket)
const welcome = await new Promise<ServerFrame | null>((resolve) => {
  const off = client.onMessage((f) => {
    if (f.type === "welcome") {
      off()
      resolve(f)
    }
  })
  client.join(key, "Verifier")
  setTimeout(() => resolve(null), 8000)
})
if (!welcome) {
  console.error("FAIL: no welcome")
  process.exit(3)
}
console.log("joined as", JSON.stringify((welcome as { you?: unknown }).you))

console.log("asking the AI Keeper a question (first call can take a while)...")
client.sendInput("你好，请用一两句话介绍一下我们所在的灯塔场景，不要掷骰子。")

// Watch up to 120s for a keeper narrative beyond the echoed input.
const deadline = Date.now() + 120_000
while (Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 1500))
  const narratives = frames.filter((f) => f.type === "narrative") as Extract<ServerFrame, { type: "narrative" }>[]
  const replies = narratives.filter((n) => n.speaker !== "player")
  if (replies.length > 0) {
    for (const n of replies) console.log(`\n=== ${n.speaker} ===\n${n.text}`)
    client.close()
    console.log("\nAI VERIFY OK: live model responded")
    process.exit(0)
  }
}
client.close()
console.error("FAIL: no AI reply within 120s")
console.error("frames seen:", frames.map((f) => f.type).join(", "))
process.exit(1)
