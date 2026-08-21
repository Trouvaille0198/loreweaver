// Mint two player invite keys via the keeper connection.
// Usage: bun run mint-keys.ts <ticket> <keeper-key>
import { IrohClient, type LoadIroh } from "./src/irohClient"
import type { ServerFrame, AdminKeysFrame } from "loreweaver-protocol"

const [ticket, key] = process.argv.slice(2)
if (!ticket || !key) {
  console.error("usage: bun run mint-keys.ts <ticket> <keeper-key>")
  process.exit(2)
}

const client = new IrohClient({ reconnect: false })
client.onMessage((f) => {
  if (f.type === "admin_keys") {
    const minted = (f as AdminKeysFrame).minted
    if (minted) {
      console.log(`MINTED: room=${minted.room} name=${minted.name} role=${minted.role} key=${minted.key}`)
    }
  }
})

await client.connect(ticket)
const welcome = await new Promise<ServerFrame | null>((resolve) => {
  const off = client.onMessage((f) => {
    if (f.type === "welcome") {
      off()
      resolve(f)
    }
  })
  client.join(key, "keeper")
  setTimeout(() => resolve(null), 8000)
})
if (!welcome) {
  console.error("FAIL: no welcome")
  process.exit(3)
}
console.log("joined as keeper; minting player keys...")

client.adminMintKey("table", "玩家A", "player", "join")
await new Promise((r) => setTimeout(r, 3000))
client.adminMintKey("table", "玩家B", "player", "join")
await new Promise((r) => setTimeout(r, 3000))
client.close()
console.log("DONE")
process.exit(0)
