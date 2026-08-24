import { describe, expect, test } from "bun:test"
import { commandHints, quickCommandHints } from "./commandCompletion"

describe("command completion", () => {
  test("matches all three command prefixes and preserves the typed prefix", () => {
    expect(commandHints(".ra", false).map((hint) => hint.display)).toContain(".ra")
    expect(commandHints("。ra", false).map((hint) => hint.display)).toContain("。ra")
    expect(commandHints("/ra", false).map((hint) => hint.display)).toContain("/ra")
  })

  test("does not offer keeper-only commands to a player", () => {
    const player = commandHints(".v", false).map((hint) => hint.display)
    const keeper = commandHints(".v", true).map((hint) => hint.display)
    expect(player).toEqual([])
    expect(keeper).toContain(".var")
  })

  test("offers skill arguments and replaces only the current token", () => {
    const hints = commandHints(".ra 侦", false)
    expect(hints[0]).toEqual({ display: "侦查", next: ".ra 侦查 " })
    expect(commandHints(".ra Spot", false)).toContainEqual({ display: "Spot Hidden", next: ".ra Spot Hidden " })
  })

  test("offers dice grammar glue instead of inventing dice values", () => {
    expect(commandHints(".r 3", false).map((hint) => hint.display)).toEqual(["d"])
    expect(commandHints(".r 3d6", false).map((hint) => hint.display)).toEqual(["kh", "kl", "+", "-"])
  })

  test("offers dynamic image nouns from the current state frame", () => {
    const hints = commandHints(".image ru", false, { npcs: ["Rusty Captain"], clues: ["ruby key"] })
    expect(hints.map((hint) => hint.display)).toEqual(["Rusty Captain", "ruby key"])
  })

  test("quick commands stay on the player surface", () => {
    const player = quickCommandHints(false).map((hint) => hint.display)
    const keeper = quickCommandHints(true).map((hint) => hint.display)
    expect(player).not.toContain(".var")
    expect(keeper).toContain(".var")
  })
})
