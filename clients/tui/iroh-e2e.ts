// E2E smoke against the REAL Iroh p2p server: dial the ticket with the same
// IrohClient the TUI uses, join with a keeper key, roll a die, assert the wire
// comes back with welcome + dice-result narrative + state.
// Usage: bun run iroh-e2e.ts <ticket> <key>
import { IrohClient } from "./src/irohClient"
import type { ServerFrame } from "loreweaver-protocol"

const [ticket, key] = process.argv.slice(2)
if (!ticket || !key) {
  console.error("usage: bun run iroh-e2e.ts <ticket> <key>")
  process.exit(2)
}

const frames: ServerFrame[] = []
const client = new IrohClient({ reconnect: false })
client.onMessage((f) => frames.push(f))

console.log("dialing ticket over Iroh QUIC...")
await client.connect(ticket)

const welcome = await new Promise<ServerFrame | null>((resolve) => {
  const off = client.onMessage((f) => {
    if (f.type === "welcome") {
      off()
      resolve(f)
    }
  })
  client.join(key, "SmokeTest")
  setTimeout(() => resolve(null), 8000)
})
if (!welcome) {
  console.error("SMOKE FAIL: no welcome frame after join")
  process.exit(3)
}
console.log("welcome:", JSON.stringify(welcome))

client.sendInput(".r 1d1+1")
await new Promise((r) => setTimeout(r, 3500))
client.close()

const narratives = frames.filter((f) => f.type === "narrative") as Extract<ServerFrame, { type: "narrative" }>[]
const hasState = frames.some((f) => f.type === "state")
const hasDice = narratives.some((f) => f.text.includes("2"))

console.log("frame types:", frames.map((f) => f.type).join(", "))
for (const n of narratives) console.log(`  narrative[${n.speaker}]: ${n.text.slice(0, 100)}`)

if (hasState && hasDice) {
  console.log("SMOKE OK: welcome + dice-result narrative (=2) + state all received over Iroh p2p")
  process.exit(0)
}
console.error(`SMOKE FAIL: welcome=${!!welcome} dice=${hasDice} state=${hasState}`)
process.exit(1)
