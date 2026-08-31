# 05 - Equipment Catalog

**Version 2.0** - amended by DR-016. Equipment is now selected from real database entries, and coefficient derivation is refused in code.

## 1. Purpose

Define the asset ontology and minimum properties required to drive simulation and analysis.

## 2. PV Module

Configuration properties:

- manufacturer/model
- rated power W
- Voc
- Vmp
- Isc
- Imp
- efficiency
- temperature coefficient of power
- nominal operating temperature assumption
- annual degradation rate
- installation date

Analytical role: establishes DC production response and degradation.

## 3. PV String / String Group

Properties:

- string ID
- parent array
- module count
- rated DC power
- orientation
- tilt/tracker
- mismatch factor
- soiling factor
- availability

Potential telemetry:

- current
- voltage
- power
- status

## 4. Combiner Box

Properties:

- combiner ID
- parent inverter
- connected string groups
- rated current
- fuse/breaker status

Potential telemetry:

- aggregate DC current
- aggregate DC voltage
- DC power
- channel imbalance
- status

## 5. Inverter

Properties:

- inverter ID
- block ID
- rated AC kW
- maximum/rated DC input
- MPPT count
- maximum DC voltage
- efficiency curve parameters
- clipping limit
- thermal derating thresholds
- standby/startup thresholds

Telemetry:

- DC voltage/current/power
- AC voltage/current/power
- frequency
- efficiency
- internal temperature
- commanded power
- operating state
- fault code

## 6. Transformer

Properties:

- transformer ID
- block ID
- rated kVA/MVA
- primary/secondary voltage
- nominal efficiency
- thermal parameters
- alarm/trip thresholds

Telemetry:

- input/output power
- loading percentage
- temperature
- voltage
- current
- status

## 7. Weather Station

Sensors:

- GHI
- DNI
- DHI where modeled
- POA irradiance
- ambient temperature
- module/backsheet temperature
- wind speed
- wind direction
- relative humidity
- optional precipitation
- optional cloud estimate

## 8. Revenue/Grid Meter

Telemetry:

- export power
- cumulative export energy
- voltage
- frequency
- optional power factor/reactive power

## 9. Breakers/Switchgear

Primarily state/event objects in V1:

- OPEN
- CLOSED
- TRIPPED
- MAINTENANCE

## 10. Plant Controller

Configuration/state:

- export limit
- curtailment command
- plant availability
- grid status
- active setpoint

## 11. Asset Identity Requirements

Every asset requires:

- stable unique ID
- asset type
- parent ID where applicable
- commissioned/installation timestamp
- rated capacity where meaningful
- enabled flag
- configuration version or dataset lineage reference

Stable IDs are essential for longitudinal time-series analysis.


## 12. Equipment Selection - DR-016

**Single-diode and Sandia parameters are looked up, never derived.**
`northstar_sim.physics.load_equipment` raises if a CEC database key is unset or
absent, rather than falling back to plausible-looking coefficients.

This is enforced because the alternative was tried and failed:
`pvlib.ivtools.sdm.fit_desoto` returned a **negative series resistance** -
physically impossible - for an invented 585 W module, and then failed to
converge on a known-good CEC entry used as a control. The routine, not the
inputs, was the problem, so tuning the datasheet was not a path forward.

| Asset | Selection | Database key |
|---|---|---|
| Module | Heliene 96M475, 477.39 W | `Heliene_96M475` |
| Inverter | Sungrow SG2500U-550V, 2,500,000 W | `Sungrow_Power_Supply_Co___Ltd___SG2500U__550V_` |

Every configuration must record its database keys. They are the lineage that
makes a dataset traceable to the equipment that produced it.

**The bundled CEC databases are a stale snapshot** topping out near 510 W
modules and 1200 V inverters. Modelling current equipment requires the live NREL
SAM libraries or manufacturer PAN files. Full rationale in
`22_equipment_and_physics_gate_record`.
