"""Shadow execution layer (Wave EXEC-S).

Records what a limit-order arm *would* have done next to the market arm actually
taken, so the D4 gate can be judged on data. Records only — this package never
places an order, never writes a live table, and never influences a decision.
"""
