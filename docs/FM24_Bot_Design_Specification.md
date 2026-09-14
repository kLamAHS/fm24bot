# FM24 Club Management Bot Design Specification

Version 1.0 | 14 September 2026 | Engineering design baseline

## Executive summary

Build a local Windows bot that manages an FM24 club through a shared sporting and financial plan. Use the existing read-only observation bridge as the primary structured data source, add verified observations for missing decisions, and operate the game through a separate UI controller. Combine explicit rules, statistical forecasts, constrained optimization, and bounded language-model assistance. Introduce learned policies only after experiments demonstrate an improvement over simple baselines.

The target is sustained performance across multiple careers: competitive results, affordable commitments, squad continuity, useful player development, and reliable execution. This specification does not assert that FM24 has been mathematically solved or that any proposed model reproduces its internal implementation.

The immediate engineering priority is a trustworthy observation and experiment platform. The first useful decision product is a combined squad planner, playing-time scheduler, and financial scenario planner. Full autonomy depends on additional observations, especially eligibility, complete contract obligations, inbox content, competition rules, and synchronized match events.

**Evidence status.** The current bridge capabilities below are reported by the supplied bridge README [S0]. Its repository, tests, and linked validation files have not been independently inspected for this specification. All additional components, numerical targets, and examples are proposed requirements rather than existing functionality or measured performance.

**Requirement language.** MUST indicates a release requirement. SHOULD indicates a recommended default with a documented exception. MAY indicates an optional experiment. Gates apply to the specific action or model requiring a capability; one missing subsystem must not disable unrelated supported work.

## 1 Product scope and operating assumptions

### 1.1 Supported environment

The initial target is FM24 on Windows x64, Steam version 24.4.2+2081827, build 18129188, with one employed human manager and one active club. The bridge is reported as tested with Python 3.14.2 x64 and exposes a loopback JSON API at http://127.0.0.1:8765. Unknown executable hashes are rejected by the bridge. Preserve that behavior.

Keep the bridge in its existing runtime. Run the bot and optional scientific libraries in a separate compatible environment, selected and pinned after dependency tests. Communicate through the JSON API so that solver or machine-learning dependencies do not destabilize the memory decoder.

The production bot operates ordinary game controls. This design does not add memory writes, injection, arbitrary-memory access, process suspension, or an editor dependency. Experimental save restoration is restricted to a separately designated laboratory career. Production history must never be silently rewound to erase a bad result.

### 1.2 Information and autonomy modes

Information mode and action authority are independent settings.

| Information mode | Allowed evidence | Evaluation treatment |
| --- | --- | --- |
| Bridge observed | Validated bridge fields, including actual player attributes, plus verified UI observations | Default research baseline; explicitly disclose privileged attributes |
| Manager visible | Only fields demonstrated to be visible to the manager at that time | Experimental until visibility masks and provenance are implemented |

Manager-visible mode MUST filter data before feature generation, retrieval, training, and action planning. Nulling only the final display is insufficient. Unknown visibility means the field is unavailable in that mode. Models trained with privileged information cannot be relabeled as manager-visible models without a new training and evaluation pipeline.

| Authority mode | Behavior |
| --- | --- |
| Observe | Record state and report status |
| Advise | Produce recommendations without game actions |
| Scoped execution | Execute user-enabled action families within saved limits |
| Club autonomy | Run the validated club workflow within a persistent authority profile |

An authority profile is configured once and persists until changed. Actions within it do not require repeated confirmation. Outside-profile actions are presented with exact terms and consequences. An unknown prerequisite blocks the dependent action even if the general action family is authorized.

### 1.3 Club management coverage

The end state covers tactics, selection, substitutions, set pieces, training, player development, scouting, recruitment, contracts, sales, loans, staff, youth pathways, board requests, registration, morale, player conversations, media, and required inbox decisions. Financial planning operates through manager-accessible choices; the bot cannot directly control sponsorship, ownership injections, or a board decision.

Optional conversations may use an explicitly configured in-game delegation policy. Mandatory decisions with missing observations must pause progression and report the specific missing capability. Merely pressing Continue does not count as managing a subsystem.

## 2 Design spec sheet

| Item | Proposed specification |
| --- | --- |
| Main objective | Improve sporting outcomes subject to operational, eligibility, and financial constraints |
| Planning horizons | Next decision; next 4 to 6 fixtures; rolling 12 months; 3-year squad succession |
| Core state store | Local SQLite with transactional writes and an append-only event journal |
| Observation source | Existing JSON bridge; typed UI observations for validated gaps |
| Action source | Separate Windows UI adapter with explicit preconditions and postconditions |
| Baseline decisions | Transparent rules and integer optimization |
| Forecasts | Regularized statistical models with uncertainty and calibration checks |
| Advanced research | Bayesian optimization, model predictive control, system identification, hierarchical reinforcement learning |
| Language model role | Interpret observed text, map available dialogue choices, summarize evidence |
| Learning policy | Train offline; release immutable model versions; no uncontrolled production exploration |
| Refresh policy | Event-driven collection with bounded polling and action-specific freshness checks |
| Failure behavior | Stop dependent actions, preserve evidence, reconcile uncertain outcomes |
| Operating visibility | Current state, next action, alternatives, constraints, model version, and execution result |
| Primary deliverable | A reproducible decision and action history tied to one career branch |

The planning horizons and implementation choices above are starting defaults. Measure throughput and predictive value before increasing complexity or shortening reaction intervals.

## 3 Existing bridge coverage and extension priorities

### 3.1 Current observation contract

[S0] reports 116 passing offline tests, 122 passing live API checks, and restart testing across 15 sampled routes. These results establish a useful starting point within the reported validation scope; they do not establish complete decoding, independent-career compatibility, or control capability.

| Route | Reported observation | Decision limitation |
| --- | --- | --- |
| /status | Connection, build, capabilities, unresolved fields | HTTP 200 can still mean disconnected |
| /game | In-game date and time | Not a career or branch identifier |
| /manager | Manager identity | One employed human manager |
| /club | Club identity and team roster | Does not establish competition eligibility |
| /squad | Roster players | May include loaned-out players |
| /players/{id} | Identity, 47 attributes, positions, morale, supported employment and readiness | No complete availability, visibility mask, or complete contract ledger |
| /finances | GBP balance, transfer budget, weekly wage budget and payroll | No complete income, expense, debt, or future-payment schedule |
| /fixtures | Calendar-year fixtures and results | Not a complete season, standings, or rules service |
| /staff | Staff identities, jobs, supported contracts | No complete ratings, workload, or responsibility model |
| /tactics | Selected tactic, mentality, validated roles and selected players | Some roles and team instructions undecoded |
| /inbox | Message metadata and unread state | No message text, attachments, or full choices |
| /training | Committed weekly calendar and supported individual settings | Some labels and current training ratings unavailable |
| /scouting | Stored reports, scout identity, dates and knowledge labels | Full prose and recommendation grades unavailable |
| /shortlists | Lists and player membership | Expiry unavailable |
| /transfer-targets | Stored target membership and supported labels | Full terms and additional statuses unavailable |
| /match | Retained score, clock, statistics, condition and starting/last positions | Timeline unclassified; active participants and events incomplete |

Player enumeration is not a guarantee of per-player decoding accuracy. The reported 26,223 identities must not be treated as 26,223 UI-validated profiles. Bulk decoder failures must not abort the entire scouting pipeline; isolate and record supported per-player results.

### 3.2 Observation extension backlog

| Priority | Required capability | Unlocks |
| --- | --- | --- |
| P0 | Explicit career registration, branch tracking, load detection and save manifest | Correct history and recovery |
| P0 | Injury, suspension, loan absence, registration and competition eligibility | Automatic selection and recruitment feasibility |
| P0 | Complete pending actions, inbox text, visible options and deadlines | Reliable calendar progression |
| P0 | Installed UI action adapter and reliable save/load workflow | Any autonomous execution or controlled experiment |
| P1 | Contract cash flows, clauses, bonuses, fees, obligations and actual payroll components | Commitment approval and financial optimization |
| P1 | Full tactical settings, legal action catalog, familiarity where visible | Tactical experiments and repeatable application |
| P1 | Verified fixture identity, event order, active players, red cards and substitution rules | Live match management |
| P1 | Competition calendar, standings, tie rules, registration rules and board objectives | Season strategy and compliance |
| P2 | Training outcomes, workload, visible medical reports, youth and loan progress | Development and recovery models |
| P2 | Staff ratings, coaching workload, facilities and board request state | Staff and infrastructure planning |
| P2 | Promises, playing-time expectations, relationships and leadership where available | Social planning and conversation decisions |
| Research | Continuous ball/player trajectories and synchronized event labels | Spatial models and possible PDE experiments |

A capability can be supplied by a verified UI adapter before a bridge decoder exists. Every such observation still requires source, timestamp, identity, and validation status. A proposed capability name in this specification is not an existing bridge endpoint.

## 4 System architecture

### 4.1 Components and boundaries

The bot is one coordinated application with logical specialist modules. It does not require separate language-model agents for every football department.

~~~text
FM24 process
  | read-only memory observations       | ordinary UI actions
  v                                    ^
Existing bridge                    UI execution adapter
  |                                    ^
  v                                    |
Observation collector -> State store -> Shared planner
                             ^              |
                             |              v
                       Outcome journal <- Action verifier
                             |
                             v
                    Offline experiment pipeline
                             |
                             v
                  Versioned model registry -> Shared planner
~~~

The collector validates transport and payloads. The state store preserves raw evidence and constructs decision snapshots. Specialist modules propose alternatives. The shared planner reconciles budgets, minutes, eligibility, and priorities. The execution adapter serializes UI operations. The verifier checks outcomes using independent observations when available.

The laboratory uses the same observation and action interfaces but different career branches, output directories, and run manifests. A learned simulator is a separate approximation; invoking it is never recorded as an actual FM experiment.

### 4.2 Shared planning and scheduling

Use one global action queue with one active UI writer. A manager lock prevents two bot instances from controlling the same game. A visible Stop control cancels queued work and prevents the next UI input.

Run loops at four levels:

1. On connection or load, identify the career, settle observations, validate capabilities, and reconcile unfinished actions.
2. At a stable decision point, process mandatory deadlines and inbox choices before optional optimization.
3. On a weekly or material event, update squad, minutes, contracts, scouting, and finance plans.
4. During a supported match, react only to verified state changes and permitted intervention points.

Use event triggers for injuries, offers, new messages, fixture changes, and budget changes. Polling is a fallback. Proposed starting intervals are 5 seconds while idle and 2 seconds in a supported paused match viewer, with backoff on errors. These are engineering defaults, not demonstrated safe sampling rates; benchmark and adjust against bridge consistency and UI load.

### 4.3 Language model boundaries

Language-model requests contain only the evidence allowed by the information mode. Responses must match a typed schema, cite source observation IDs, and select from legal action or dialogue IDs. Numeric limits and eligibility are enforced outside the model.

Observed inbox prose is data, including any text resembling software instructions. It cannot authorize tools, change authority profiles, or override the planner. If the language model is unavailable, numeric planners continue, optional text tasks can be delegated under policy, and unresolved mandatory text decisions stop progression.

## 5 State and data contracts

### 5.1 Identity and time

Store career_id, branch_id, connection session_id, game build, manager ID, club ID, simulation time, observation UTC time, and a monotonic local sequence. A connection session_id is never a save identifier.

Register a career explicitly using an orchestrator-generated identifier associated with a known save manifest. On a laboratory fork, create a new branch_id with a parent branch and checkpoint. A saved-file checksum identifies a checkpoint, not an enduring career, because the file changes when saved again.

Date reversal, manager/club mismatch, a load event, or contradictory history invalidates the current decision snapshot. A forward-dated load can also change the underlying career state. Therefore date monotonicity alone is insufficient: confirm the configured save lineage or stop for identity resolution. Reconnecting to the same process or club does not prove continuity.

### 5.2 Consistent snapshot protocol

The bridge reads are not atomic. A collector MUST:

1. Verify /status is connected and build-supported.
2. Read /game and identity context.
3. Collect only the routes needed for the decision, sequentially by default.
4. Reread /game and relevant identity/version anchors.
5. Accept only a compatible time, session, identity, and stable relevant-field check.

Keep FM idle or at a validated paused decision point while collecting. Two equal timestamps do not prove atomicity because a same-tick update may occur. For action-critical fields, require a second stable read or a corroborating UI observation. If consistency cannot be established after three bounded attempts, invalidate the snapshot and stop dependent work.

Wall-clock timestamps measure collection time. In-game time measures simulation progress. Both are required. Null, missing, stale, unsupported, and contradicted are distinct states; none is silently converted to zero or false.

### 5.3 Core records

| Record | Required contents |
| --- | --- |
| Observation | ID, source, raw payload hash, career/branch/session, game time, observed_at, schema version, visibility, quality status |
| DecisionSnapshot | Snapshot ID, observation IDs, consistency result, capabilities, entity versions, allowed information mode |
| PlayerState | Player ID, valid attributes and units, positions, employment, observed morale/readiness, separately sourced eligibility |
| FinancialCommitment | Counterparties, currency, amount, due date, recurrence, trigger, payer, certainty, source and version |
| CompetitionContext | Competition ID, stage, fixture identity, squad rules, substitution rules, deadlines and source |
| Promise | Player/staff ID, observed commitment, deadline, progress, consequences if known, source choice and confidence |
| Decision | Objective version, input snapshot, candidates, constraints, forecast intervals, selected action and reasons |
| ActionIntent | Action ID, authority scope, targets, parameters, preconditions, expiry, verification plan and idempotency key |
| ActionResult | Attempt count, before/after evidence, execution state, confirmed effect or uncertainty, recovery instruction |
| Experiment | Hypothesis, treatment, controls, branch/checkpoint, randomization, policy version, outcomes and validity flags |
| ModelVersion | Feature schema, training lineage, information mode, build scope, calibration results, artifact hash and release state |

Amounts MUST carry currency and payment period. Use exact decimal or integer minor-unit arithmetic for money. Retain native GBP for decisions and weekly units for wage data; convert only in an explicitly labeled presentation layer. Do not add a weekly rate to a monthly obligation without a calendar-aware conversion.

### 5.4 Storage and interfaces

Use SQLite tables for careers, branches, observations, snapshots, decisions, action_intents, action_attempts, commitments, promises, experiments, and model_versions. Enforce foreign keys. Save large immutable evidence blobs separately by content hash. Database migrations and feature schema changes require explicit version increments.

The following are proposed internal interfaces, not bridge API routes:

~~~text
collect(requirements, context) -> DecisionSnapshot | Unavailable
propose(snapshot, objective, limits) -> CandidateDecision[]
evaluate(candidate, scenarios) -> ForecastAndConstraintReport
authorize(intent, authority_profile) -> Allowed | OutsideScope
execute(intent) -> ActionAttempt
verify(intent, before, after) -> Confirmed | Failed | Uncertain
reconcile(action_id, fresh_snapshot) -> RecoveryDecision
~~~

Training queries must perform as-of joins: a feature is available only when the decision could have observed it. Store the time an outcome became known separately from the event's simulation date. This prevents future scout reports, later readiness, and retrospectively completed match statistics from leaking into earlier decisions.

## 6 Mathematical decision framework

### 6.1 Model of the game

Treat FM as an unknown controlled system:

~~~text
x[t+1] = f(x[t], action[t], randomness[t])
observation[t] = h(x[t], observation_process[t])
belief[t] = P(x[t] | allowed observations and actions through t)
~~~

The full state includes unobserved game variables. The belief is an operational estimate, not a claim that a complete POMDP has been solved. A practical implementation may use a small set of uncertain quantities and scenario samples rather than a distribution over every game variable. Partial observability requires history and uncertainty management [S1].

Maintain separate horizons and models. Match interventions, weekly recovery, contract cash flows, and multi-year development do not share a useful single time step. Couple them through shared resources and terminal forecasts, not by forcing all processes into one enormous neural network.

### 6.2 Objectives and constraints

First enforce hard execution, eligibility, and authority constraints. Then optimize sporting value within the configured financial risk limits. Development, continuity, and resale can influence long-term sporting value, but must not compensate for an illegal lineup or an unauthorized commitment.

An illustrative objective for feasible plans is:

~~~text
maximize E[sporting_value over horizon]
       + w_dev * normalized_development_value
       + w_cont * normalized_squad_continuity
       - w_risk * normalized_downside_loss
       - w_change * unnecessary_plan_changes
~~~

Weights and normalization scales belong to a versioned club objective profile. The profile states competition priorities, relegation/promotion targets, cash reserve policy, and development emphasis. Never add raw pounds, points, and morale scores without an explicit conversion or normalization. Report the separate components so a high score cannot hide a sporting failure.

Use legal and observed requirements as hard constraints. Use preferences and uncertain promises as penalized constraints where appropriate. If existing circumstances are already infeasible, report the conflict and produce a recovery plan; do not pretend that new constraints can undo inherited debt or injuries.

### 6.3 Forecast standards

Begin with transparent baselines: rolling averages, opponent-adjusted regressions, role scores, explicit accounting, and conservative rules. Compare each advanced model against the best relevant baseline.

Forecasts MUST include their training scope, uncertainty method, known missing inputs, and validity period. Calibrate predicted probabilities and intervals on held-out careers or periods. A model that recognizes an unfamiliar league, club state, or changed build should widen uncertainty or defer to a supported baseline.

Model confidence is not observation validity. A statistical model cannot turn an unverified active-player list into a confirmed lineup.

## 7 Squad construction and selection

### 7.1 Role valuation and lineup assignment

Create a role-specific baseline score from available attributes, positions, and verified readiness. Initially use explicit, reviewable weights with no claim that they match the engine. Estimate context effects and role interactions only after sufficient varied data exists.

For player p, role r, and fixture f, define x[p,r,f] as a binary assignment. Core constraints include:

~~~text
sum over players x[p,r,f] = required players in role r
sum over roles x[p,r,f] <= 1 for each player and fixture
x[p,r,f] <= verified_eligible[p,f]
planned_minutes[p,f] <= permitted_minutes[p,f]
~~~

Represent bench composition, substitutes, registered squads, homegrown rules, and competition-specific limits separately. Do not assume a universal squad size, bench size, or substitution rule. Constraint programming is well suited to assignments with availability and workload restrictions [S2].

Solve the next 4 to 6 fixtures jointly where data allows. Link accumulated minutes to recovery forecasts and playing-time expectations. The immediate action is the next lineup; future lineups are revisable plans.

Future availability is scenario-dependent. Apply known competition and contract restrictions to future plans, model uncertain absences explicitly, and reverify current eligibility before submission. A future plan must not imply that a player is already confirmed fit for that future date.

### 7.2 Recruitment and portfolio effects

Evaluate each candidate by marginal contribution to the existing squad and future plan. Include fees, wages, agent/signing costs, bonuses, development minutes, availability, plausible acceptance, and registration feasibility. A displayed valuation is neither an executable buying price nor guaranteed resale income.

Use binary acquisition, sale, retention, and loan variables with role coverage and financial constraints. Candidate packages should expose tradeoffs rather than present one unexplained ranking. Recompute the plan when negotiation terms change.

Versatility value is the expected reduction in performance loss across availability scenarios. A player can cover only one simultaneous assignment. Avoid counting their full insurance value independently for every position.

The value of an additional squad place or a scarce role can be estimated by resolving the optimization with and without that resource. This is a marginal value under the model, not an objective market price.

### 7.3 Inputs, outputs and failure behavior

Inputs are verified eligible players, tactical role options, fixture context, supported attributes, workload evidence, contract costs, and objective limits. Outputs are a legal lineup, bench, minutes plan, candidate recruitment packages, succession gaps, and binding constraints.

If eligibility is missing, emit an advisory candidate lineup labeled as unverified; do not submit it. If a solver times out, use its last feasible incumbent only after an independent constraint check. If no feasible solution exists, identify the conflicting requirements and stop submission.

## 8 Finance and negotiation

### 8.1 Cash flow engine

Model actual cash movements over a rolling 12-month calendar, using finer intervals around major commitments and transfer windows:

~~~text
cash[t+1, scenario] = cash[t, scenario]
                    + receipts[t, scenario]
                    - payments[t, scenario]
~~~

Classify each movement as observed committed, conditional, forecast, or unknown. Payroll already included in an aggregate must not be added again through player contracts. Reconcile the ledger against observed balance changes; residual differences remain explicitly unexplained.

Model cash balance, transfer budget, wage budget, and any observed regulatory limits as separate constraints. Match each commitment to its actual payment dates. An installment reduces immediate cash outflow but creates a future obligation.

Use scenario planning for promotion, remaining in the division, relegation where relevant, cup progress, player-sale uncertainty, and conditional bonuses. Do not assume owner funding or a successful sale to make a deal feasible.

### 8.2 Risk policy and rolling optimization

Replan after material events and implement only the current decision, following the logic of model predictive control [S3]. An example risk constraint is:

~~~text
P(minimum future cash >= configured reserve) >= 1 - epsilon
~~~

This probability is only as credible as the scenario model and its calibration. A scenario pass rate is not an externally guaranteed solvency probability. Where probabilities are weak, require survival of explicit stress scenarios and label the resulting policy accordingly.

Conditional value at risk MAY summarize the mean loss in the worst fraction of scenarios. Tail fraction, reserve, and risk tolerance are configurable, not universal football constants. Existing shortfalls trigger a recovery objective that seeks to reduce commitments and improve feasible options.

### 8.3 Negotiation controller

Create a reservation package before negotiating: maximum total commitment, acceptable payment schedule, allowed clauses, intended playing time, and a walk-away condition. Parse every changed offer into the commitment model.

A multi-step negotiation is a state machine with visible offer versions. The bot may propose counteroffers within its stored authority profile. Final acceptance requires exact terms, fresh budgets, valid counterparties, and a confirmed offer version. Unrecognized clauses or ambiguous payer obligations stop acceptance while supported planning continues.

Sale decisions include replacement cost and squad damage, not only proceeds. Loan decisions include fee and wage allocations, observed clauses, expected use, and development opportunity. Uncertain future proceeds receive explicit scenarios rather than reducing guaranteed costs at face value.

### 8.4 Fractional differentiation experiment

Fractional differentiation is a candidate transformation for longitudinal financial features [S4]:

~~~text
z[t] = sum from k=0 to K of w[k] * series[t-k]
w[0] = 1
w[k] = -w[k-1] * (d-k+1) / k
~~~

Use a finite window K and differentiation order d chosen inside training folds. Compare d=0, d=1, seasonal changes, and fractional alternatives. Retain raw levels and event/calendar features when needed. Do not take logarithms of nonpositive balances. Repeated observations of an unchanged balance are not independent financial samples.

The admission test is improved out-of-sample forecasts and better decisions after transaction timing, leakage controls, and complexity costs. A stationarity test alone is insufficient. If history is too short or structural breaks dominate, keep the accounting baseline.

## 9 Tactics and live match control

### 9.1 Tactical policy representation

Represent a tactic as a versioned set of legal formation positions, role/duty assignments, mentality, team instructions, player instructions, and set-piece routines. Record which settings are observed, controllable, or unavailable.

Start from a small catalog of coherent legal systems. Tune bounded groups of settings, with constraints against incompatible combinations and excessive switching. Familiarity and switching costs are modeled only when measurable or conservatively approximated and labeled.

Use Bayesian optimization for expensive, noisy tactical experiments [S5]. Record candidate settings, opponent context, starting squad, observation validity, and execution confirmation. Evaluate on a range of opponents and clubs so the result does not merely exploit one fixture or one squad.

Set-piece optimization is a separate candidate space for takers, aerial threats, defensive assignments, and routines. FM24's attribute-based set-piece assignments make this a meaningful subsystem, but the required routines and event labels are not currently fully exposed [S8].

### 9.2 Match state and action policy

The minimum live state includes fixture identity, authoritative event order, score, match phase, active participants, dismissals, verified substitutions remaining, available bench, and fresh decision-relevant fitness evidence.

The current /match feed does not meet this complete requirement. Until timing is classified, retained statistics must not trigger live actions or be joined to an earlier replay moment. Store them as timeline-unclassified observations. Starting opposition positions are not evidence of the current formation.

A baseline match policy chooses among a bounded set of prevalidated actions at verified intervention points. Candidate value includes expected result, future player availability, tactical disruption, and the option value of remaining substitutions. Apply a cooldown and require material evidence before repeated tactical changes.

The last_position field cannot establish who is on the pitch. Completed-pass share must retain its proxy label; do not rename it true possession. Virtual-player statistics with unresolved identity remain separate observations and cannot identify a substitution target.

### 9.3 Reinforcement learning research

Hierarchical reinforcement learning MAY eventually select a tactical policy or intervention from the legal catalog. The action controller and hard constraints remain outside the learned policy.

Treat substitutions and tactical changes as sequential decisions with delayed effects. A contextual bandit is acceptable only for an explicitly bounded experiment whose feedback and carryover assumptions have been justified; it is not the default model of an entire match.

Offline policy evaluation requires logged action probabilities and adequate support for alternative actions. If the historic behavior never tried a candidate, a reliable counterfactual score cannot be assumed. Use simulator trials or controlled experiments to acquire evidence.

## 10 Training recovery and development

### 10.1 Initial scheduling model

The baseline balances committed training, fixture density, verified condition/sharpness, role preparation, and development priorities. It uses explicit rest and workload rules until enough longitudinal outcomes exist.

Condition does not prove injury status, readiness does not prove eligibility, and a current cached value is not a medical diagnosis. The model is about in-game state only. Missing injury or workload evidence prevents claims of calibrated injury risk.

Record individual focus, intensity, role/position work, training calendar, minutes, observed outcomes, age, and relevant context. Development rewards should reflect sustained useful improvement and squad contribution, not a single noisy training rating.

### 10.2 ODE and discrete state experiments

A candidate continuous fatigue approximation is:

~~~text
dF/dt = alpha * workload(t) - beta * F(t)
F(after match) = F(before match) + match_load
~~~

F is a latent model variable, not automatically the bridge condition percentage. Fit an observation model connecting F to validated measurements. Match-load jumps and calendar events produce a hybrid model. Compare it with a simpler daily state update and an empirical recovery curve.

Estimate parameters with bounded, interpretable assumptions. Pool information across players where necessary but preserve individual uncertainty. If parameter values cannot be identified from the available measurements, do not deploy a falsely precise player-specific model.

Sparse system identification, including SINDy with control inputs, MAY search for compact nonlinear terms [S6]. It requires informative state and input measurements. Rounded, stale caches and infrequent observations can make derivative estimates unreliable; use discrete formulations or better measurements rather than forcing an ODE fit.

PDE research remains deferred until spatial trajectories, synchronization, and a useful managerial decision target exist. A field model of pitch pressure is not needed to schedule training or manage finances.

### 10.3 Youth retraining and loans

Use uncertain development trajectories to compare first-team minutes, reserve/youth opportunities, retraining, mentoring, and loans. A route is valuable only if the required facilities, staff, registration, and actual opportunities can be observed or bounded.

Treat retraining as an investment with duration, opportunity cost, and probability of becoming useful in the target role. Monitor actual loan participation rather than assuming promised playing time occurs. Revisit development plans on meaningful evidence, not every small attribute fluctuation.

## 11 Scouting staff and club relationships

### 11.1 Scouting and information value

Maintain estimates of candidate contribution, affordability, availability, and uncertainty. Choose assignments by expected value of information:

~~~text
VOI = expected best decision value after obtaining evidence
    - best decision value with current evidence
    - scouting cost and opportunity cost
~~~

This is a model-based estimate of decision improvement. In bridge-observed mode, scouting may still reveal missing terms or context, but the bot must not claim it is discovering attributes it already knows.

Use report dates and source quality when updating beliefs. Absence of a stored report is not proof that a player is unknown. Respect information-mode restrictions and label unsupported recommendation grades as unavailable.

### 11.2 Staff and facilities

Compare staffing or board requests by the bottleneck they relieve: coaching workload, recruitment coverage, player recovery information, set pieces, or youth opportunity. Include wages, capacity limits, and time until benefit.

A board request is an action with an uncertain response, not an immediately executed upgrade. Record pending requests and actual outcomes. If staff quality, workload, or facilities are unavailable, provide a missing-evidence report instead of invented numerical returns.

### 11.3 Promises morale and conversations

Maintain a promise ledger with exact observed terms, parties, deadlines, progress, and source. Check it before recruitment, renewals, selection, and player conversations. Playing-time commitments must consume forecast minutes in the shared plan.

An optional relationship graph may represent observed leadership and relationships. Do not infer a complete social network from morale alone. Estimate knock-on effects only when repeated evidence supports them.

For dialogue and media, the language model ranks observed legal choices using club policy, evidence, and existing commitments. It cannot invent a free-text response when the game offers only fixed options. FM24's targets and interaction design connect these decisions with contracts, loans, and player context [S9].

### 11.4 Competition and board strategy

Maintain a versioned rules profile for each competition and stage. It includes registration windows, eligibility, squad composition, tie progression, substitution restrictions, and deadlines. Refresh after promotion, a season boundary, or a changed competition context.

Prioritize competitions using explicit club objectives and uncertainty about future benefits. Board expectations, job-security evidence, and required meetings enter the planner when observed. No proxy such as current league position should silently substitute for an unread board requirement.

## 12 Action execution and recovery

### 12.1 Controller requirements

Implement a Windows adapter using accessibility elements where available and verified screen recognition where necessary. FM-specific support must be tested; accessibility availability is not assumed. Screenshot/OCR results require a known screen, stable identity, and corroboration for consequential terms.

Each supported UI workflow has a versioned screen model, legal actions, expected transitions, timeouts, and recovery states. Validate display scaling, window geometry, language, skin, and input focus. A change invalidates affected workflows until checked.

Use ordinary in-game pause controls where supported. Do not suspend the process. The adapter must never send inputs when another application has focus or when the game screen is unidentified. Human input cancels or pauses the active operation at the next safe boundary.

### 12.2 Action lifecycle

~~~text
PROPOSED -> VALIDATED -> QUEUED -> EXECUTING -> VERIFYING -> CONFIRMED
                |                     |            |
                v                     v            v
          OUTSIDE_SCOPE             FAILED       UNCERTAIN
                                                   |
                                                   v
                                            RECONCILING
~~~

Persist an intent before sending the first input. Immediately before execution, check career, branch, source object version, legal action, authority, required capabilities, and action-specific freshness. Expire intents when their relevant context changes, even if the game date remains the same.

Verification must establish the intended effect. A changed screen or a successful click is insufficient. For selection, verify the selected player IDs and roles. For training, reread committed settings. For contracts, verify the accepted agreement and resulting obligations. Use exact machine values where available and documented tolerances only for display rounding.

### 12.3 Idempotency and uncertainty

An idempotency key prevents the bot from intentionally dispatching the same intent twice, but cannot make a UI click intrinsically idempotent. If the result is uncertain, reconcile observed state before any retry.

Low-risk navigation can have bounded retries after a fresh screen check. Offer submission, contract acceptance, player release, conversation confirmation, and Continue must not be blindly retried. If the system cannot determine whether an action took effect, stop that workflow and preserve the before/after evidence.

Use compensating UI actions only when a known reversal is permitted and its effects are understood. Production recovery does not restore an old save to remove consequences. Laboratory restoration is a separately logged experiment operation.

### 12.4 Progression and reconnection

Before Continue, verify no unresolved mandatory choice, lineup error, registration deadline, or pending action requires intervention. Record the expected next decision boundary. After progression, settle and collect a new snapshot.

On 503, disconnect, or build mismatch, stop action execution and mark affected observations unavailable. On restart, reconcile persisted EXECUTING and VERIFYING intents before resuming. Never silently reset them to QUEUED.

After a load, discard live-match caches and reidentify the fixture and participants. The bridge's reported restart checks do not establish same-match resume correctness. Same-match restart behavior is a dedicated future acceptance test.

## 13 Experimental platform and statistical evaluation

### 13.1 Laboratory contract

The laboratory runner requires actual, validated observation, action, save, restore, and progression workflows. The current read-only bridge alone cannot perform rollouts, parallel simulations, or self-play.

Every run records game/build hash, relevant database or mod configuration, loaded leagues, simulation detail settings, club/manager, career lineage, starting checkpoint, policy/model versions, authority profile, treatment, elapsed wall time, and result validity.

Use separate laboratory saves with immutable starting checkpoints. A trial manifest records the parent checkpoint and all known differences. One game instance is assumed initially; additional instances require explicit capability and license/resource validation before being added to the runner.

### 13.2 Controlled experiments

State the hypothesis, treatment, outcome, smallest useful effect, and stopping rule before running a comparison. Examples include whether a pressing change improves goal difference for a given squad class, or whether a recovery policy preserves readiness without reducing useful development.

Compare policies from matched starting checkpoints when practical, counterbalance order, and vary independent career starts and contexts. Determine whether save restoration repeats random behavior. Do not assume access to a seed setter or that pressing reload creates independent outcomes.

Never treat polling records as experimental replicates. Fixtures within a season and branches from the same career may be correlated. Analyze at the appropriate run/career level and use paired comparisons or clustered uncertainty where justified.

Multiple tactical changes create a treatment package. To estimate individual causes, use a controlled factorial or sequential design with sufficient variation and explicit interaction terms. Ordinary correlations between attributes and performance are not automatically causal effects.

### 13.3 Data splits and leakage controls

Use separate training, tuning, and final evaluation sets. Group branches from the same starting career together when testing generalization. Also evaluate chronological extrapolation with past-only training and an embargo at overlapping outcome windows where necessary.

Freeze candidate models and thresholds before the final evaluation. Keep a final set of untouched careers for release claims. Resetting a model after seeing those results consumes that test set; subsequent claims require a new holdout.

Reject any feature containing later scout knowledge, final-match statistics at an earlier decision time, future contracts, or information from another branch. Scalers, missing-value transforms, feature selection, fractional-differencing parameters, and outcome-derived labels are fitted within training folds only.

### 13.4 Metrics and baselines

| Area | Primary measurements | Relevant baseline |
| --- | --- | --- |
| Sporting performance | Points per match, goal difference, competition outcomes, objective completion | Fixed legal heuristic policy and available in-game delegation |
| Finances | Reserve shortfalls, forecast error, committed costs, missed obligations | Explicit accounting and fixed reserve rules |
| Selection | Eligibility violations, role coverage, readiness, planned versus actual minutes | Greedy legal role selection |
| Development | Sustained attribute trajectory, useful minutes, contribution, retention | Fixed development and loan policy |
| Predictions | Calibration, proper scoring rules, interval coverage, error by context | Simple historical or regularized model |
| Execution | Confirmed actions, wrong-target events, duplicates, uncertain outcomes, recovery time | Verified scripted workflow |
| Operations | Full workflow completion, blocked mandatory decisions, wall time per run, model cost | Previous released bot |

Use only metrics actually available and validated. Expected goals, true possession, continuous tracking, complete injury data, and player value histories are not assumed present in the current bridge.

Distinguish technical invalidity from poor performance. Wrong-save identity or failed treatment application can invalidate a trial. An injury, defeat, unsuccessful sale, or board rejection is normally part of the outcome and must not be discarded for being inconvenient.

### 13.5 Evidence gates

Begin with a pilot to estimate variability and simulation throughput. Determine the evaluation size from the smallest useful effect, clustering, and available compute; do not choose a convenient match count and assume it is sufficient.

A proposed release candidate must pass all correctness gates and show no material degradation on declared financial and execution guardrails. For a sporting-improvement claim, require the prespecified confidence interval for improvement over the primary baseline to clear the chosen threshold on held-out data. If the experiment is underpowered, classify the result as inconclusive.

A model can still be released as an experimental advisory feature when its uncertainty and scope are explicit. It cannot become the autonomous default merely because it is more sophisticated.

## 14 Functional requirements and acceptance tests

| ID | Requirement | Acceptance test |
| --- | --- | --- |
| OBS 01 | Respect bridge connection and build status | Disconnected HTTP 200 and unsupported-build fixtures disable actions |
| OBS 02 | Preserve missing-value semantics | Null eligibility, stale readiness and absent money remain unavailable |
| OBS 03 | Reject inconsistent snapshots | Inject time/session/identity changes and same-tick relevant-field changes |
| ID 01 | Preserve career and branch lineage | Reload earlier and later checkpoints; no cross-branch history merge |
| VIS 01 | Enforce information mode before inference | Privileged attributes cannot enter restricted features, retrieval or training |
| FIN 01 | Preserve money units and timing | Calendar and currency fixtures detect weekly/monthly errors and double counting |
| FIN 02 | Bound complete commitments | A deal within transfer budget but outside cash limits is rejected |
| SEL 01 | Submit only verified legal selections | Players confirmed ineligible through injury, suspension, loan or registration cannot be submitted |
| SEL 02 | Report infeasibility | Impossible coverage produces conflicting constraints and no fabricated lineup |
| ACT 01 | Validate target and fresh context | Changed player/offer/screen invalidates queued action |
| ACT 02 | Prevent uncertain duplicate effects | Simulated timeout after successful acceptance causes reconciliation, not retry |
| ACT 03 | Respect human control | Focus loss and Stop prevent subsequent UI input |
| REC 01 | Resume safely after interruption | Persisted in-flight action is reconciled after restart |
| MAT 01 | Gate live decisions on timeline validity | Unclassified retained statistics cannot trigger substitutions |
| MAT 02 | Verify participants and match rules | Last-position remnants and unknown substitutes block dependent actions |
| CAL 01 | Handle mandatory game workflow | Unread required decision blocks Continue until resolved |
| EXP 01 | Produce reproducible manifests | Every trial maps to checkpoint, build, policy, treatment and outcomes |
| EXP 02 | Prevent future-information leakage | As-of and branch-isolation checks fail deliberately contaminated datasets |
| MOD 01 | Gate model releases | Failed calibration or unsupported feature schema falls back to baseline |
| AUD 01 | Explain every executed action | Every result resolves to inputs, limits, decision version and evidence |

Offline contract and fault-injection tests precede live UI trials. Keep existing bridge regression tests separate from bot tests and preserve their save-specific expectations.

For each newly enabled consequential workflow, a proposed engineering gate is 100 varied successful end-to-end cases, including injected recovery failures, with zero wrong-target, duplicate-commitment, or authority-violation events. This is a regression gate, not proof of a universal failure rate; correlated repetitions do not establish independent reliability.

Before club-autonomy release, demonstrate a complete competitive season with transfers, registration, training, contracts, inbox decisions and matches on the supported configuration. Separately run cross-career benchmarks for performance claims. Workflow completion and competitive superiority are different acceptance criteria.

## 15 Runtime operations and interface

### 15.1 Local operator interface

The operator sees the registered career, information mode, authority scope, connection state, current game date, next proposed action, unresolved prerequisites, and Stop control.

Each decision view shows the selected action, strongest alternatives, expected outcomes with uncertainty, binding constraints, and source timestamps. Detailed evidence is expandable. The main flow uses football language rather than solver or database terminology.

Offer simple controls for competition priorities, spending limits, cash reserve, development emphasis, permitted action families, and optional staff delegation. Preserve these settings across restarts and include their version in decisions.

Notify on meaningful events: required user action, unsupported mandatory workflow, a material plan change, completion, or failure. Do not produce a message for every unchanged poll.

### 15.2 Performance and resource targets

Initial engineering targets on the chosen development machine are under 5 seconds for a normal off-match snapshot and simple lineup plan, and under 30 seconds for a bounded recruitment scenario solve. These are budgets to benchmark, not current measurements.

Long optimization runs leave the game at a safe stable point and show progress. Use solver time limits and independently validate feasible incumbents. Match decisions are restricted to validated paused intervention points in early releases; no real-time latency guarantee is assumed.

Cache immutable identities and model artifacts by version. Avoid repeatedly decoding every player. Limit language-model calls to text-heavy decisions and material plan explanations. Record tokens or monetary cost where a provider exposes them, with user-configured limits.

### 15.3 Persistence and maintenance

Commit intent and outcome transitions transactionally. Use append-only evidence records and a single journal writer. Back up configuration, manifests, and the database before schema migrations; do not overwrite user saves as an incidental backup operation.

A game update, decoder change, feature-schema change, UI skin change, or model replacement has a compatibility check. Models retain their validated build and context scope. Read-only collection may continue when safe, but affected actions stay disabled until regression checks pass.

Routine logs use IDs and typed fields. Screenshots, inbox content, and saves can contain user-controlled material and remain local by default. An external model provider receives only the minimum authorized context. No external account, service, or upload is required by the baseline architecture.

## 16 Implementation plan

### 16.1 Package layout

~~~text
fm_bot/
  bridge_client/       transport, schema adapters, capability checks
  state/               identities, snapshots, units, visibility, journal
  rules/               eligibility, competitions, deadlines, authority
  planning/            squad, minutes, finances, recruitment, strategy
  models/              forecasts, calibration, dynamics, registry
  execution/           screen models, actions, verification, reconciliation
  interactions/        inbox parsing, choice mapping, promises
  experiments/         checkpoints, treatments, manifests, evaluation
  interface/           operator status, explanations, controls
  tests/               contract, fault, solver, leakage, live workflows
~~~

Prefer a small initial dependency set: an HTTP client, typed validation, SQLite, numerical arrays, and a tested integer solver. Candidate later tools include OR-Tools for assignments, SciPy for fitting, a regularized modeling library for forecasts, and BoTorch for Bayesian optimization. Confirm Windows and interpreter compatibility before selecting and pinning exact versions. These are proposed dependency choices, not an installation performed by this specification.

### 16.2 Delivery phases

| Phase | Deliverables | Exit gate |
| --- | --- | --- |
| 0 Observation foundation | Career/branch identity, raw journal, consistent snapshots, visibility and unit handling | OBS, ID and VIS acceptance tests pass |
| 1 Advisory club planner | Legal candidate selection, minutes plan, partial finance model, missing-capability report | Useful traceable advice; unavailable prerequisites visibly block execution |
| 2 Controlled execution | Screen adapter, authority profile, action journal, postcondition verification, Stop | Supported workflow tests and interruption/recovery tests pass |
| 3 Experimental laboratory | Validated checkpoints, treatment application, manifest, progression, outcome collection | Reproducible trials and leakage checks pass |
| 4 Shared sporting and finance planning | Complete commitment ledger, recruitment packages, training baseline, scouting decisions | Financial and selection gates pass on live scenarios |
| 5 Match and social coverage | Verified events, substitutions, set pieces, inbox choices, promises, registration | Complete supported competitive workflow with no mandatory gaps |
| 6 Model improvement and autonomy | Calibrated models, held-out benchmarks, immutable releases and fallbacks | Season-completion gate plus evidence for each performance claim |

Phases may overlap where dependencies permit. For example, financial observation work can proceed during UI adapter development. The plan is capability-driven; calendar estimates require measurements of decoder effort, UI reliability, and simulation throughput.

### 16.3 First implementation backlog

| Ticket | Priority | Concrete output |
| --- | --- | --- |
| BOT 001 | P0 | Bridge client that records schema/version, errors and source payloads |
| BOT 002 | P0 | Career registry and checkpoint/branch manifest with reload tests |
| BOT 003 | P0 | Snapshot validator with typed freshness and consistency statuses |
| BOT 004 | P0 | Exact money units, obligation types and reconciliation primitives |
| BOT 005 | P0 | Capability registry and dependency-based action blocking |
| BOT 006 | P0 | Persistent intent journal, authority profile and single-writer control |
| BOT 007 | P1 | Eligibility observation adapter and competition rules fixtures |
| BOT 008 | P1 | Baseline lineup and minutes optimizer with infeasibility explanations |
| BOT 009 | P1 | First validated UI workflow with independent postcondition checks |
| BOT 010 | P1 | Laboratory save/restore and treatment runner |
| BOT 011 | P1 | Baseline cash-flow planner and complete-term negotiation parser |
| BOT 012 | P2 | Calibration, as-of feature pipeline and one preregistered model experiment |

Start by proving a complete narrow loop: identify career, collect stable state, propose one supported change, apply it, verify it, and recover from an injected interruption. Broad autonomous behavior depends on this loop working reliably.

## 17 Worked decision and contract examples

### 17.1 Recruitment decision

The following is a synthetic example of planner behavior, not an FM prediction.

The squad planner identifies weak left-back coverage. Candidate A improves the starting lineup but consumes most available wage capacity. Candidate B provides less immediate improvement and covers an additional role. The finance planner evaluates actual proposed terms under both ordinary and adverse scenarios. The minutes planner checks existing commitments.

If Candidate A requires promotion income to meet the configured reserve rule, the plan is infeasible under that policy. Candidate B may remain feasible. The decision report presents the sporting tradeoff, financial scenario results, role coverage, uncertain acceptance, and maximum authorized package. Negotiations must still obtain an executable offer before the bot can accept anything.

### 17.2 Example action intent

This illustrative internal JSON describes a tactic selection from a previously validated catalog. The identifiers are synthetic. A catalog entry must map to actual observed settings and a tested UI workflow.

~~~json
{
  "schema_version": 1,
  "action_id": "example-action-001",
  "career_id": "example-career",
  "branch_id": "example-main",
  "decision_snapshot_id": "example-snapshot-042",
  "kind": "select_validated_tactic",
  "parameters": {
    "tactic_catalog_id": "example-balanced-01",
    "catalog_version": 3
  },
  "required_capabilities": [
    "tactic_catalog_readback",
    "tactic_selection_ui"
  ],
  "authority_scope": "tactics.select",
  "preconditions": [
    "same_career_and_branch",
    "verified_stable_decision_point",
    "unchanged_relevant_tactic_state"
  ],
  "expires_on": "relevant_state_change",
  "verification": "selected_tactic_matches_catalog",
  "idempotency_key": "example-main:example-decision-042"
}
~~~

The executor creates a separate attempt record. Confirmed execution requires a fresh readback. If the screen changes unexpectedly after selection, the result becomes UNCERTAIN until reconciliation establishes the actual setting.

### 17.3 Example evidence record

~~~json
{
  "model_id": "example-recovery-baseline-v1",
  "information_mode": "bridge_observed",
  "source_snapshot_id": "example-snapshot-042",
  "forecast_target": "validated_readiness_at_next_fixture",
  "point_estimate": null,
  "interval": null,
  "status": "unavailable",
  "reason": "required_current_readiness_observation_missing"
}
~~~

An unavailable estimate is a valid result. The planner must not replace it with a confident score merely to satisfy an interface.

## 18 Design risks and decisions

| Risk | Required response |
| --- | --- |
| Hidden or misdecoded state | Capability gates, source quality, conservative fallbacks and targeted validation |
| UI changes or focus loss | Versioned workflows, fresh recognition, Stop and no blind input |
| Model exploits incomplete observations | Feature lineage, independent outcome measures and adversarial evaluation cases |
| Overfitting one save or tactic | Grouped holdouts, varied contexts, declared hypotheses and frozen evaluations |
| Repeated randomness after reload | Characterize repeatability and count effective experimental units honestly |
| Long training or simulation time | Benchmark throughput, use simple baselines and bounded experiment budgets |
| Financial forecast misses obligations | Full-term parsing, reconciliation residuals and commitment-specific action gates |
| Cross-module conflict | One shared plan for money, minutes, squad places and deadlines |
| Good forecasts produce poor decisions | Evaluate downstream outcomes and regret, not prediction accuracy alone |
| Excessive caution blocks the career | Track mandatory capability gaps, prioritize adapters, permit configured delegation |
| Advanced math adds complexity without benefit | Baseline comparison, model admission gates and reversible release selection |

The default research mode uses actual bridge attributes and discloses that advantage. The initial runtime is local, serial, and advisory. Full autonomy is an end-state milestone, not a capability implied by having a read-only API.

The first research candidates are role valuation, fixture-aware minutes planning, and financial scenarios. Fatigue ODEs, fractional differentiation, SINDy, spatial PDEs, and reinforcement learning remain individually gated experiments. No advanced method is a prerequisite for a useful first release.

## 19 References and evidence map

[S0] User-supplied FM24 read-only observation bridge README, received in this conversation. Identifies the supported build, reported validation, route contracts, observation semantics and unresolved fields. The linked repository research files were not provided as source contents for this specification.

[S1] Anthony R. Cassandra. [Background on POMDPs](https://www.pomdp.org/tutorial/pomdp-background.html). Supports the partial-observability framework; does not establish an FM-specific solution.

[S2] Google OR-Tools. [Employee Scheduling](https://developers.google.com/optimization/scheduling/employee_scheduling). Supports constrained assignment and scheduling methods; football rules must be supplied and validated separately.

[S3] Xinyue Shen and Stephen Boyd. [Incremental Proximal Multi-Forecast Model Predictive Control](https://web.stanford.edu/~boyd/papers/ip_mf_mpc.html). Supports rolling planning across forecast scenarios.

[S4] Marcos Lopez de Prado. [Fractionally Differentiated Features in Advances in Financial Machine Learning](https://www.oreilly.com/library/view/advances-in-financial/9781119482086/c05.xhtml). Supports the candidate transformation; this specification proposes an FM-specific evaluation rather than assuming benefit.

[S5] BoTorch. [Overview](https://botorch.org/docs/overview). Supports Bayesian optimization of expensive and noisy objectives.

[S6] Steven L. Brunton, Joshua L. Proctor and J. Nathan Kutz. [Sparse Identification of Nonlinear Dynamics with Control](https://arxiv.org/abs/1605.06682). Supports data-driven discovery of controlled dynamics under suitable measurement assumptions.

[S7] Steven L. Brunton, Joshua L. Proctor and J. Nathan Kutz. [Discovering governing equations from data](https://arxiv.org/abs/1509.03580). Background on sparse dynamical model discovery and its assumptions.

[S8] Sports Interactive. [Set Pieces Refresh and Coaches Debut](https://www.footballmanager.com/features/set-pieces-refresh-and-coaches-debut), 29 September 2023. FM24 feature description.

[S9] Sports Interactive. [Individual Player Targets and Interaction Logic](https://www.footballmanager.com/features/individual-player-targets-and-interaction-logic), 22 September 2023. FM24 feature description.

The mathematical references support methods and terminology. They do not validate the proposed FM24 models, objectives, parameter values, or performance. Those claims require the experiments and release gates specified above.
