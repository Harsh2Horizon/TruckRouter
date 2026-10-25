# Fuel Optimization: Mathematical Model and Algorithms

| API | Procedure |
|---|---|
| `POST /api/fuel/greedy/` | Next-cheaper station with detour-adjusted prices |
| `POST /api/fuel/dijkstra/` | Standard state-space Dijkstra |

**Request example:**

```json
{
  "origin": {"latitude": 40.7128, "longitude": -74.0060},
  "destination": {"latitude": 34.0522, "longitude": -118.2437},
  "corridor_miles": 5,
  "detour_multiplier": 1.4
}
```

## 1. Shared Model

**Per valid request:** One OSRM route call → one indexed PostGIS corridor query → projection → optimization → physical fuel ledger. No routing retries, redirects, or station-routing calls.

### 1.1 Constants and Station Data

$$
C = 50\ \mathrm{gal}, \qquad
\eta = 10\ \mathrm{mi/gal}, \qquad
R = \eta C = 500\ \mathrm{mi}, \qquad
f_0 = C.
$$

$$
0 < w \leq 25\ \mathrm{mi}, \qquad 1 \leq \alpha \leq 5.
$$

Use locations with stored coordinates; deduplicate by location ID. Prices must be finite, non-negative, and comparable in a common currency per US gallon.

$$
p_i = \max\left\{\text{retail price observations for location } i\right\}.
$$

### 1.2 Route Projection and Detours

| Symbol | Meaning |
|---|---|
| $\Gamma = (v_0, \ldots, v_m)$ | OSRM route geometry |
| $L$ | OSRM driving distance, miles |
| $s_i, q_i$ | Station location and nearest route-segment projection |
| $h_i$ | Cumulative spherical distance to the projection |
| $x_i, d_i$ | Route-mile position and **one-way** detour |
| $w, \alpha$ | Corridor radius and detour multiplier |

Distances use Haversine miles (`hav`); projection uses spherical segments and a bounding-volume index.

$$
\begin{aligned}
q_i &= \underset{q \in \Gamma}{\operatorname{argmin}}\; \operatorname{hav}(s_i,q), \\
H &= \sum_{r=0}^{m-1} \operatorname{hav}(v_r,v_{r+1}), \\
x_i &= L\frac{h_i}{H}, \\
d_i &= \alpha\operatorname{hav}(s_i,q_i).
\end{aligned}
$$

PostGIS selects stations within $w$ miles using geography-based `ST_DWithin`. Sort nodes by `(route mile, location ID)`; equal-distance projections use the earliest position. A zero-length route requires no fuel.

**Accessibility is approximate:** Geometric detours cannot verify ramps, barriers, road direction, or truck access. Returned GeoJSON remains the base route; each location has one forward route position.

### 1.3 Forward Graph and Objective

Let node $0$ be the origin and node $t$ be the destination:

$$
x_0 = 0, \qquad x_t = L, \qquad d_0 = d_t = 0.
$$

$$
D_{ij} = x_j - x_i + d_i + d_j, \qquad
c_{ij} = \frac{D_{ij}}{\eta}.
$$

$$
E = \left\{(i,j) : i < j,\; D_{ij} \leq R\right\}.
$$

Remove nodes that cannot reach $t$ through these edges. Let $A_i$ contain the remaining forward neighbors of node $i$.

For selected stops $P$, minimize **additional fuel purchases**. Initial fuel has no acquisition cost; no terminal reserve is required:

$$
\boxed{\min \sum_{i \in P} p_i u_i}
$$

Subject to:

$$
\begin{aligned}
f_j^- &= f_i^+ - c_{ij} \geq 0, \\
f_i^+ &= f_i^- + u_i \leq C, \\
u_i &\geq 0, \qquad f_0^+ = C.
\end{aligned}
$$

Here $f_i^-$ and $f_i^+$ are arrival and departure fuel, respectively. Inbound detour fuel is consumed **before** purchasing; outbound detour fuel is consumed **after** purchasing.

## 2. Greedy Next-Cheaper Station

### 2.1 Detour-Adjusted Comparison Price

$$
\widetilde{p}_i = p_i\left(1 + \frac{2d_i}{\eta C}\right), \qquad
\widetilde{p}_0 = 0, \qquad
\widetilde{p}_t = -\infty.
$$

Cheaper reachable nodes:

$$
J_i = \left\{j \in A_i : \widetilde{p}_j < \widetilde{p}_i\right\}.
$$

Purchase policy at a real station:

$$
u_i =
\begin{cases}
\max(0,c_{ij}-f),
& j = \min J_i,\quad J_i \neq \varnothing, \\
C-f,
& j = \operatorname{argmin}_{r \in A_i}(\widetilde{p}_r,d_r,r),\quad J_i=\varnothing.
\end{cases}
$$

### 2.2 Pseudocode

```text
i ← origin; f ← 50; purchases ← []
while i ≠ destination:
    if c(i, destination) ≤ f: return purchases
    if A[i] is empty: UNREACHABLE

    J ← forward nodes in A[i] with adjusted_price < adjusted_price[i]
    if J is nonempty:
        j ← first node in J
        target_fuel ← c(i, j)
    else:
        j ← argmin (adjusted_price, detour, node_order) in A[i]
        target_fuel ← 50

    u ← 0 at origin; otherwise max(0, target_fuel − f)
    record positive purchase u at i
    f ← f + u − c(i, j)
    i ← j
```

**Heuristic:** Adjusted prices amortize access over 50 gallons. Stop selection is approximate; expenditure always uses the original price $p_i$.

## 3. Standard State-Space Dijkstra

### 3.1 State and Transitions

$$
\delta = 0.5\ \mathrm{gal}, \qquad K = \frac{C}{\delta} = 100.
$$

$$
S = \left\{(i,k) : k \in \{0,\ldots,K\}\right\}, \qquad
s_{\mathrm{start}} = (0,K), \qquad
S_{\mathrm{goal}} = \{(t,k)\}.
$$

**Purchase** at a real station:

$$
(i,k) \longrightarrow (i,k+1), \qquad
\mathrm{cost} = \delta p_i, \qquad k < K.
$$

**Drive** with conservatively rounded consumption:

$$
q_{ij} = \left\lceil\frac{c_{ij}}{\delta}\right\rceil.
$$

$$
(i,k) \longrightarrow (j,k-q_{ij}), \qquad
\mathrm{cost} = 0, \qquad
(i,j) \in E,\quad k \geq q_{ij}.
$$

### 3.2 Distance Relaxation and Search

Each exact state $s=(i,k)$ has an independent minimum cost $d[s]$:

$$
d[(0,K)] = 0, \qquad d[s] = \infty \quad\text{otherwise}.
$$

For a transition $s \to s'$ with cost $w$:

$$
\boxed{g' = d[s]+w < d[s']\ \Longrightarrow\ d[s'] \gets g'.}
$$

Different fuel levels are never compared or discarded against each other.

```text
if destination is reachable on the initial tank: return no purchases

d[(origin,100)] ← 0; all other distances ← infinity
heap ← [(0, origin, 100)]; settled ← empty set

while heap is nonempty:
    (g, i, k) ← pop minimum cost
    if (i,k) is settled or g ≠ d[(i,k)]: continue  # stale heap entry
    mark (i,k) settled

    if i = destination:
        follow parent states
        aggregate purchase actions
        return

    for each feasible purchase or driving transition to state s':
        g' ← g + transition_cost
        if g' < d[s']:
            d[s'] ← g'
            parent[s'] ← ((i,k), action)
            push (g', s') onto heap

UNREACHABLE
```

Non-negative weights and `Decimal` costs give the **optimal cost in this discrete graph**. Each state is settled once. The implementation stores a predecessor/action per state and uses an insertion counter for deterministic heap ties. Zero-purchase intermediate visits can be removed because skipping them saves detour mileage.

### 3.3 Physical Replay of the Discrete Plan

Per-leg rounding reserve:

$$
0 \leq \delta q_{ij} - c_{ij} < \delta.
$$

Actual remaining fuel may exceed its modeled balance. Prevent physical overfilling:

$$
u_i^{\mathrm{actual}} =
\min\left(u_i^{\mathrm{planned}},\ C-f_i^{-,\mathrm{actual}}\right).
$$

This preserves feasibility and cannot increase cost. Actual purchases may therefore be fractional; `discrete_objective_cost` and `physical_purchase_reduction_gallons` expose the adjustment. Continuous real-road optimality is not claimed.

## 4. Outputs, Limits, and Verification

For selected stops $P$:

$$
\begin{aligned}
D_{\mathrm{detour}} &= 2\sum_{i \in P} d_i, \\
D_{\mathrm{total}} &= L + D_{\mathrm{detour}}, \\
f_t &= C + \sum_{i \in P} u_i^{\mathrm{actual}} - \frac{D_{\mathrm{total}}}{\eta}, \\
\operatorname{expenditure} &= \sum_{i \in P} p_i u_i^{\mathrm{actual}}.
\end{aligned}
$$

Both APIs return `route` GeoJSON, ordered `stops`, prices, gallons, costs, arrival/departure fuel, `summary`, `algorithm_execution_time_ms`, `candidate_count`, assumptions, and diagnostics. Timing includes graph construction/search. Costs use six decimal places; total cost sums rounded stop-cost lines.

### 4.1 Complexity

With $N$ stations and $E$ forward edges, graph construction and subsequent searches have bounds:

$$
T_{\mathrm{build}} = O(N\log N + E), \qquad
T_{\mathrm{greedy}} = O(E).
$$

$$
V = (N+2)(K+1), \qquad
E_s \leq NK + (K+1)E.
$$

$$
T_{\mathrm{Dijkstra}} = O\left((V+E_s)\log(V+E_s)\right), \qquad
M_{\mathrm{Dijkstra}} = O(V+E_s).
$$

### 4.2 Defaults and Resource Limits

| Setting | Default |
|---|---|
| Corridor radius | **5 mi** |
| Detour multiplier | **1.4** |
| Maximum candidates | **1,000** |
| Maximum discovered states (`FUEL_MAX_SEARCH_STATES`) | **250,000** |
| OSRM timeout | **15 s** |

See [`.env.example`](.env.example). Resource limits fail explicitly; candidate sets are not silently truncated.

### 4.3 API Error Responses

| HTTP code | Meaning |
|---|---|
| **400** | Invalid input |
| **405 / 415** | Unsupported method / content type |
| **422** | Unreachable route / candidate limit |
| **502** | Routing failure |
| **503** | Database / search limit |

### 4.4 Setup and Tests

```sh
# PostgreSQL 17 + PostGIS; existing named volume retained.
docker compose build postgres
docker compose up -d --wait postgres
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py runserver

# Both algorithms, real PostGIS queries, mocked OSRM, and existing geocoding.
.venv/bin/python manage.py test geocoding.tests fuel_optimization.tests --noinput
```

Tests cover reachability, zero-refuel trips, detour trade-offs, fuel rounding/capacity, independent fuel states, stale heap entries, projection, and one-call/one-query execution. Dijkstra is compared against an independent reference solver.
