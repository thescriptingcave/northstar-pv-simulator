# 04 - Physical Architecture

**Version 2.0** - amended after Phases 4 and 5. Balance-of-plant models and the plant controller are now implemented; the additions below are normative.

## 1. Energy Flow

Sunlight
-> PV modules
-> strings
-> combiner/string aggregation
-> inverter DC input
-> inverter AC output
-> transformer
-> medium-voltage collection
-> substation
-> revenue meter
-> grid point of interconnection

## 2. Information Flow

Environmental conditions
-> weather sensors

Asset sensors
-> inverter/transformer/string telemetry

Equipment state
-> controller/SCADA concept

Fault/state transitions
-> events and alarms

All telemetry/events
-> output dispatcher
-> time-series storage / files / streaming destination

## 3. Power Block

The power block is a major analytical boundary. It should contain enough assets that an entire block can be compared against peer blocks.

Each block should expose:

- installed DC capacity
- installed AC capacity
- aggregate DC power
- aggregate AC power
- transformer loading
- operational state
- active inverter count
- expected power
- actual power
- estimated loss

## 4. PV Arrays and Strings

Arrays define physical grouping and orientation.

Strings represent series-connected modules. For computational efficiency, the simulator may aggregate strings into telemetry groups while retaining string-count and module-count configuration.

Important analytical properties:

- orientation
- tilt/tracker association
- module type
- string count
- installed DC power
- soiling factor
- degradation factor
- availability

## 5. Inverter Boundary

The inverter is a primary telemetry and analysis object.

It is where:

- DC input is aggregated
- MPPT behavior is represented
- DC-to-AC conversion occurs
- clipping can occur
- thermal derating can occur
- electrical faults can trip production

## 6. Transformer Boundary

Transformers provide:

- AC power transfer
- loading percentage
- electrical losses
- temperature behavior
- thermal alarms
- block-level outage mechanism

## 7. Grid Boundary

The point of interconnection defines:

- exported real power
- optional reactive power
- grid voltage
- frequency
- export limit
- curtailment
- grid-connected/disconnected state

## 8. Control Boundary

A conceptual plant controller should be able to impose:

- plant export limit
- inverter setpoint/derating
- curtailment
- shutdown
- restart sequence

This creates important distinctions between available solar resource and commanded output.


## 9. Balance of Plant - as implemented (Phase 4)

### 9.1 Transformer

Losses are **load-dependent**, not a fixed percentage. A constant-efficiency
transformer produces a flat efficiency curve, which eliminates the
efficiency-variation analysis in `02 §5` and makes health monitoring impossible.

| Component | Behaviour |
|---|---|
| No-load loss | Constant whenever energised |
| Load loss | Scales with the square of loading |
| Winding temperature | First-order lag on loading, 45-minute constant |

**Output is negative in darkness.** No-load loss is present whenever the
transformer is energised, and utility PV plants keep theirs energised overnight,
so the plant imports station service. Clipping output at zero discards that
energy and breaks the loss chain by 0.14% of daily export.

Measured overnight plant export: **-120 kW** - forty inverters at -0.75 kW
standby plus ten transformers at -9 kW no-load. This appears at the revenue
meter and is real.

### 9.2 AC Collection

Resistive loss scaling with the square of loading, 0.8% at rated output.
Measured on a clear day: transformer 0.56%, collection 0.64% of inverter AC
energy.

### 9.3 Plant Controller - as implemented (Phase 5)

The controller distributes an export limit as per-inverter setpoints. The limit
applies at the **meter**; the setpoints apply at the **inverters**; losses sit
between them, so available inverter power is converted to expected export
before the limit is applied.

Reduction is shared **pro rata**. Real controllers use various strategies, but a
uniform reduction keeps the per-asset signature uniform, so curtailment is not
mistaken for one inverter underperforming.

An inverter held below what it could produce is relabelled **`CURTAILED`**, not
left as `RUNNING` at lower output. Without the relabel, curtailment is
indistinguishable from underperformance in the state column.

**Note: the POI limit does not currently bind.** With the limit equal to AC
nameplate and losses downstream, plant export never reaches it. Many real plants
are built with POI capacity *below* inverter nameplate, which makes meter-side
clipping a routine daily event. Lowering `poi_export_limit_kw` is the single
parameter if that is wanted.

### 9.4 Implementation Constraint: Shared Geometry

Solar position, tracker orientation, angle of incidence, airmass and the
bifacial ground view factors depend only on location, time and tracker geometry
- **not** on the irradiance an individual asset receives. Computing them per
inverter cost 0.373 s per inverter-day; sharing them costs 0.022 s.

A 17-fold reduction, moving a simulated year from 91 minutes to 13.6. Any
implementation must compute them once per timestep, not once per asset.
