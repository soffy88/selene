export function divergenceKind(localQty: number, venueQty: number): "ok" | "ghost_local" | "ghost_venue" | "qty" {
  if (localQty > 0 && venueQty === 0) return "ghost_local";
  if (localQty === 0 && venueQty > 0) return "ghost_venue";
  if (localQty !== venueQty) return "qty";
  return "ok";
}
