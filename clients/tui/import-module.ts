// Keeper-side module import via the live p2p server: enable the module skill,
// import the two world cards, and import the standalone lorebook.
// Usage: bun run import-module.ts <ticket> <key>
import { IrohClient } from "./src/irohClient"
import type { ServerFrame } from "loreweaver-protocol"

const [ticket, key] = process.argv.slice(2)
if (!ticket || !key) {
  console.error("usage: bun run import-module.ts <ticket> <key>")
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
  client.join(key, "import-bot")
  setTimeout(() => resolve(null), 8000)
})
if (!welcome) {
  console.error("FAIL: no welcome")
  process.exit(3)
}
console.log("joined as keeper; running imports...\n")

const lastNarrative = (): string => {
  const ns = frames.filter((f) => f.type === "narrative") as Extract<ServerFrame, { type: "narrative" }>[]
  return ns.length ? ns[ns.length - 1].text : ""
}

async function send(cmd: string, waitMs: number, doneIf: (text: string) => boolean): Promise<void> {
  const before = frames.length
  client.sendInput(cmd)
  const deadline = Date.now() + waitMs
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2500))
    const text = lastNarrative()
    if (text && doneIf(text)) return
  }
}

const commands: Array<{ cmd: string; waitMs: number; label: string; done: (t: string) => boolean }> = [
  {
    cmd: ".skill enable yingchao-zhuchi",
    waitMs: 20_000,
    label: "enable skill yingchao-zhuchi",
    done: (t) => /enable|启用|skill/i.test(t),
  },
  {
    cmd: ".import xipu-songdeng/cards/shipu.lorecard.json world",
    waitMs: 360_000,
    label: "import world card shipu.lorecard.json",
    done: (t) => /已导入|导入完成|Imported|failed|失败|错误|拒绝|denied/i.test(t),
  },
  {
    cmd: ".import xipu-songdeng/cards/chaomai-st.json world",
    waitMs: 300_000,
    label: "import world card chaomai-st.json",
    done: (t) => /已导入|导入完成|Imported|failed|失败|错误|拒绝|denied/i.test(t),
  },
  {
    cmd: ".lore import xipu-songdeng/lorebooks/yuyan.json",
    waitMs: 60_000,
    label: "import lorebook yuyan.json",
    done: (t) => /已导入|导入完成|Imported|failed|失败|错误|拒绝|denied/i.test(t),
  },
]

for (const c of commands) {
  console.log(`>> ${c.label}`)
  await send(c.cmd, c.waitMs, c.done)
  // print narratives that arrived since this command
  const ns = frames.filter((f) => f.type === "narrative") as Extract<ServerFrame, { type: "narrative" }>[]
  for (const n of ns.slice(-6)) {
    console.log(`   [${n.speaker}] ${n.text.slice(0, 200)}`)
  }
  console.log("")
}

client.close()
console.log("IMPORT RUN DONE")
process.exit(0)
