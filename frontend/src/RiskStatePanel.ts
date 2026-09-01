export function riskTone(halted: boolean, circuitOpen: boolean): "ok" | "halt" {
  return halted || circuitOpen ? "halt" : "ok";
}
