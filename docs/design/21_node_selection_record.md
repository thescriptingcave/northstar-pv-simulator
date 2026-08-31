# 21 - Node Selection Record (DR-015)

## DR-015 - Plant Pricing Proxy - LOCKED (provisionally)

**Decision:** `node_settlement_point = "HRNT_SLR_RN"`

| Attribute | Value |
|---|---|
| Load zone | `LZ_WEST` |
| Interconnection voltage | 345 kV |
| Substation | `OZTSW` |
| Registered units | 3 (UNIT1, UNIT2, UNIT3) |
| Electrical buses at substation | 7 |
| Co-located storage | None |
| Source | ERCOT `Settlement Points List and Electrical Buses Mapping`, EMIL `np4-160-sg`, extract dated 2026-08-05 |

Locked **provisionally**: the selection is defensible from the network model, but price-history coverage cannot be established from that extract and is verified empirically in §5.

---

## 1. What the Node Is For

The plant resource node determines **basis** — the spread between the price at the plant's own node and the price at `HB_WEST`, the hedge index under `18 §3`.

Basis is the only price component structurally correlated with the plant's own output. West Texas nodes go deeply negative relative to the hub precisely when every solar plant in the region is generating at once, which is exactly when NorthStar generates. That correlation is the analytical content; a node chosen badly produces a basis series that is merely noise, and it fails silently.

NorthStar is not a model of the plant behind this node. The node is a **pricing proxy** — standard practice, and recorded as such in the cache manifest.

---

## 2. Method Corrected

An earlier approach — grepping the extract for commercial plant names (Roserock, Buckthorn, East Pecos, Roadrunner) — was wrong and produced false positives:

| Grep | Hit | Reality |
|---|---|---|
| `ROSE` | `ROSELAND_ALL` | LZ_NORTH, 345 kV, solar + storage. Not Roserock |
| `BUCKTH` | `BUCKTHRN_RN` | LZ_NORTH, 138 kV. Not the Pecos County Buckthorn |
| `PECOS`, `MAPLE`, `EPEC`, `ROADRUN` | none | — |

**ERCOT resource node names are not derived from the names a developer markets a project under.** Name matching is not a valid selection method and any node found that way must be treated as unverified.

The selection is now a reproducible filter implemented in `northstar_fetch.settlement_points`, runnable as:

```bash
northstar-fetch select-node <unpacked-extract-dir>
```

---

## 3. Selection Criteria

Applied to all 965 resource nodes in the extract; 11 survive.

| Filter | Rationale |
|---|---|
| `load_zone == LZ_WEST` | The plant's congestion zone. 256 of 965 nodes qualify |
| `interconnection_kv >= 300` | A 100 MW plant interconnects at transmission voltage. A 138 kV node is a materially smaller facility |
| No `ESR`/`BESS`/`BATT` units | **A hybrid node's price reflects charge and discharge behaviour NorthStar does not have.** Importing storage arbitrage into a standalone PV plant's basis would be a modelling error, not a conservatism |
| Solar marker in node name | Convenience filter only — see §4 |

Ranking: unit count nearest 3 (the scale a ~100 MW single-site plant typically registers), then larger bus count as a tiebreak, on the reasoning that a better-built-out site is more likely to have long, continuous price history.

**Result:**

| Rank | Node | Units | Buses | Substation |
|---|---|---|---|---|
| 1 | `HRNT_SLR_RN` | 3 | 7 | OZTSW |
| 2 | `MLB_SLR_RN` | 3 | 6 | QUASAR |
| 3 | `BUZI_SLR_RN` | 4 | 7 | WRASW |
| 4 | `CST2_SLR_RN` | 2 | 6 | CST2_SLR |
| 5 | `FAGUSSLR_RN` | 2 | 5 | FAGUSSLR |
| … | | | | |
| 11 | `GRYH_SLR_RN` | 8 | 14 | METSW |

`GRYH_SLR_RN` ranks last despite being the largest. Eight registered units is a facility several times NorthStar's size, whose own output contributes materially to local congestion — not a representative proxy for a 100 MW plant.

All 11 candidates share a 34.5 kV collection to 345 kV interconnection topology, which matches NorthStar's own architecture in `04 §1`. That is a useful corroboration that the filter is selecting the right class of asset.

---

## 4. Known Limitations

**The solar-name filter is incomplete.** 233 of the 256 `LZ_WEST` nodes carry no fuel marker in the name. ERCOT does not require node names to indicate technology, so this filter identifies a convenience subset and certainly misses solar plants. Pass `--any-name` to widen it.

**County cannot be established from the extract.** It carries no coordinates. Confirming a node sits in Pecos County requires an external join — EIA-860 (free, annual) supplies plant name, county and lat/lon, matched against substation codes.

This is deliberately not treated as blocking. What matters for a pricing proxy is that the node *behaves* like a West Texas solar node, which §5 establishes directly from price history. Exact county is secondary.

**Price history coverage is unknown.** The extract is a current snapshot with a 31-day display duration. A node only exists in price history after its plant energizes, so a node commissioned in 2022 has no 2019 prices. This is the main residual risk and the reason the lock is provisional.

---

## 5. Empirical Validation - Required Before Full Fetch

Fetch one year for the selected node and confirm:

1. **Coverage** — data exists across all of 2019–2024. Partial coverage means fall back to the next candidate.
2. **Negative midday prices** — present, and clustered in high-irradiance hours.
3. **Basis is non-zero** — the node series diverges from `HB_WEST`. Identical series means a hub alias was selected, not a resource node.
4. **Basis widens during high-solar hours** — the correlation this whole decision exists to capture.

If checks 2–4 fail, the node is not a solar-congested West Texas node and the next candidate is tried.

---

## 6. Fallback Order

1. `HRNT_SLR_RN` — selected
2. `MLB_SLR_RN` — equal scale, slightly smaller build-out
3. `BUZI_SLR_RN` — one unit larger
4. `CST2_SLR_RN` — one unit smaller

**Final fallback:** if no candidate has adequate history, set `node_settlement_point = "HB_WEST"` and declare basis zero for V1.

This is the correct degradation, and it is worth being explicit about why: **synthetic basis is not an acceptable substitute.** Basis is congestion-driven and correlated with regional solar output. A fabricated series would have neither property, and would teach a relationship that does not exist. Losing one analysis visibly is better than gaining a fictitious one. Settlement, economic curtailment and loss attribution all continue to work with zero basis.

---

## 7. Consequences

- `config/northstar.toml` sets `node_settlement_point = "HRNT_SLR_RN"`.
- The choice, and the extract vintage it came from, are recorded in the cache manifest per `19 §5.2`.
- Changing the node after a price fetch requires a `cache_version` bump: the price partitions are keyed by settlement point, and mixing nodes across a dataset would corrupt basis.
- `19 §11`'s "ERCOT settlement point selected" checklist item is satisfied provisionally, and closes fully when §5 passes.
