// JSON-lines driver for a pinned aps-checkout-gate checkout.
//
// The runner starts this with `node driver/gate_driver.ts <gate-dir>` and talks
// to it one JSON object per line on stdin and stdout. Every call goes to the
// gate's own code: Gate.open, decide, reportOutcome, reconcile, revoke and
// verifyDecision from <gate-dir>/src, and the agent-passport-system SDK the gate
// pins in <gate-dir>/node_modules. The driver adds no checks of its own. It only
// gives the gate a controlled clock and deterministic test keys, so a retained
// run can be compared byte for byte with the next one.
//
// The runner kills this process with SIGKILL to simulate a gate crash; the next
// driver opens the same sqlite file and the gate's own startup recovery runs.

import { createInterface } from 'node:readline'
import { createHash, createPrivateKey, createPublicKey } from 'node:crypto'
import { pathToFileURL } from 'node:url'
import { join, resolve } from 'node:path'

const gateDir = resolve(process.argv[2] ?? '')
const load = (rel: string) => import(pathToFileURL(join(gateDir, rel)).href)

const gateMod = await load('src/gate.ts')
const decisionMod = await load('src/decision.ts')
const requestMod = await load('src/request.ts')
const sdk = await load('node_modules/agent-passport-system/dist/src/index.js')
let verdictMod: any = null
try { verdictMod = await load('src/verdict.ts') } catch { verdictMod = null }

const clock = { t: 0 }
let gate: any = null
let gateParty: any = null
let directory: any = null

/** Raw 32-byte Ed25519 key pair from a seed string, in the SDK's hex form. */
function partyFrom(id: string, role: string, seedLabel: string) {
  const seed = createHash('sha256').update(seedLabel, 'utf8').digest()
  const der = Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed])
  const priv = createPrivateKey({ key: der, format: 'der', type: 'pkcs8' })
  const pub = createPublicKey(priv).export({ type: 'spki', format: 'der' }) as Buffer
  return { id, role, privateKey: seed.toString('hex'), publicKey: pub.subarray(pub.length - 32).toString('hex') }
}

const parties = new Map<string, any>()

function party(id: string): any {
  const p = parties.get(id)
  if (!p) throw new Error(`unknown party ${id}`)
  return p
}

function trusted(ids: string[]) {
  const out: Record<string, { role: string; publicKey: string }> = {}
  for (const id of ids) { const p = party(id); out[id] = { role: p.role, publicKey: p.publicKey } }
  return { format: 'aps-checkout-gate/trusted-keys-v1', parties: out }
}

const iso = (ms: number) => new Date(ms).toISOString()

const ops: Record<string, (a: any) => any> = {
  parties(a) {
    for (const p of a.parties) parties.set(p.id, partyFrom(p.id, p.role, p.seed))
    return Object.fromEntries([...parties.values()].map(p => [p.id, { role: p.role, publicKey: p.publicKey }]))
  },
  open(a) {
    clock.t = a.now
    gateParty = party(a.gate)
    directory = trusted(a.trusted)
    const evidenceChecks: any[] = []
    for (const c of a.evidence_checks ?? []) {
      if (c.type !== 'verdict') throw new Error(`unknown evidence check ${c.type}`)
      if (!verdictMod) throw new Error('this gate checkout has no src/verdict.ts')
      evidenceChecks.push(verdictMod.verdictCheck({ verifierPubkey: c.verifierPubkey, proceedOn: c.proceedOn }))
    }
    gate = gateMod.Gate.open({
      dbPath: a.db, gate: gateParty, directory, now: () => new Date(clock.t),
      ...(a.recovery_window_ms ? { recoveryWindowMs: a.recovery_window_ms } : {}),
      ...(evidenceChecks.length ? { evidenceChecks } : {}),
    })
    return { recovered: gate.recovered, gate_public_key: gateParty.publicKey, key_id: gate.keyId }
  },
  grant(a) {
    clock.t = a.now
    const principal = party(a.principal)
    const agent = party(a.agent)
    // The same fields as the gate's own issueGrant (src/delegation.ts), with the
    // nonce supplied, so the delegation_id is the same on every run.
    return sdk.issueAuthorityDelegation({
      record_type: 'aps:authority-delegation:v1', version: '1.0', parent_delegation_id: null,
      issuer: principal.id, subject: agent.id, verification_method: `${principal.id}#key-1`,
      issued_at: iso(a.now), nonce: a.nonce,
      authority: {
        scope: { profile: 'aps-hierarchical-v1', grants: [`checkout:${a.merchant}:purchase`] },
        spend: { mode: 'bounded', unit: `iso4217:${a.currency}:minor`, per_action: String(a.per_action), cumulative: String(a.cumulative) },
        depth: { remaining: 0 },
        time: { not_before: iso(a.now), not_after: iso(a.now + a.ttl_ms) },
        reputation: { profile: 'aps-score-0-100-v1', ceiling: 100 },
        values: { profile: 'aps-values-identifiers-v1', required: [] },
        reversibility: { profile: 'aps-tci-v1', ceiling: 'compensable' },
      },
    }, principal.privateKey)
  },
  intent(a) {
    return requestMod.signIntent(a.request, party(a.agent), a.delegation, new Date(a.now))
  },
  digest(a) {
    return requestMod.requestDigest(a.request)
  },
  decide(a) {
    clock.t = a.now
    const r = gate.decide({ request: a.request, delegation: a.delegation, intent: a.intent, ...(a.evidence ? { evidence: a.evidence } : {}) })
    return { record: r.record, verdict: r.verdict, reason: r.reason, reservation_id: r.reservation_id, replay: r.replay }
  },
  outcome(a) {
    clock.t = a.now
    return gate.reportOutcome(a.reservation_id, a.outcome, a.confirmation)
  },
  reconcile(a) {
    clock.t = a.now
    return gate.reconcile(a.reservation_id, { outcome: a.outcome, confirmation: a.confirmation })
  },
  revoke(a) {
    clock.t = a.now
    gate.revoke(a.delegation_id, a.reason)
    return { ok: true }
  },
  reservation(a) {
    return gate.reservation(a.reservation_id) ?? null
  },
  events(a) {
    return gate.db.prepare('SELECT from_state, to_state, cause, evidence, at FROM reservation_events WHERE reservation_id = ? ORDER BY event_id').all(a.reservation_id)
  },
  verify(a) {
    return decisionMod.verifyDecision(a.record, a.gate_id, a.public_key)
  },
  verdict_problems(a) {
    if (!verdictMod) throw new Error('this gate checkout has no src/verdict.ts')
    return verdictMod.verdictProblems(a.event, a.request, a.action_ref, new Date(a.now), a.cfg)
  },
  /** A synthetic reviewer verdict, signed the way the gate's own test 11 signs one
   *  (BIP-340, aux = 32 zero bytes, key from a label). Test material only: it
   *  stands in for a reviewer service the run must not call. */
  sign_test_verdict(a) {
    if (!verdictMod) throw new Error('this gate checkout has no src/verdict.ts')
    const C = verdictMod.secp256k1ForTests
    const hex32 = (n: bigint) => Buffer.from(n.toString(16).padStart(64, '0'), 'hex')
    const d0 = C.toBig(C.taggedHash('test/seed', Buffer.from(a.label))) % C.N
    const P = C.mul(C.G, d0)
    const pub = hex32(P[0]).toString('hex')
    const content = JSON.stringify({
      verdict: a.verdict, verified_at: Math.floor(a.at / 1000),
      action_binding_args_hash: 'sha256:' + requestMod.requestDigest(a.request), action_binding_agent_id: a.request.agent_id,
    })
    const ev: any = { pubkey: pub, created_at: Math.floor(a.at / 1000), kind: 30078, tags: [], content, id: '', sig: '' }
    ev.id = verdictMod.eventId(ev)
    const msg = Buffer.from(ev.id, 'hex')
    const d = P[1] % 2n === 0n ? d0 : C.N - d0
    const t = Buffer.from(hex32(d).map((b: number, i: number) => b ^ C.taggedHash('BIP0340/aux', Buffer.alloc(32))[i]))
    const k0 = C.toBig(C.taggedHash('BIP0340/nonce', Buffer.concat([t, hex32(P[0]), msg]))) % C.N
    const R = C.mul(C.G, k0)
    const kk = R[1] % 2n === 0n ? k0 : C.N - k0
    const e = C.toBig(C.taggedHash('BIP0340/challenge', Buffer.concat([hex32(R[0]), hex32(P[0]), msg]))) % C.N
    ev.sig = Buffer.concat([hex32(R[0]), hex32((kk + e * d) % C.N)]).toString('hex')
    return { event: ev, pubkey: pub }
  },
  close() {
    gate?.close()
    gate = null
    return { ok: true }
  },
}

const rl = createInterface({ input: process.stdin })
for await (const line of rl) {
  if (!line.trim()) continue
  let reply: unknown
  try {
    const msg = JSON.parse(line)
    const fn = ops[msg.op]
    if (!fn) throw new Error(`unknown op ${msg.op}`)
    reply = { ok: true, result: fn(msg.args ?? {}) }
  } catch (e) {
    reply = { ok: false, error: e instanceof Error ? e.message : String(e) }
  }
  process.stdout.write(JSON.stringify(reply, (_k, v) => (typeof v === 'bigint' ? v.toString() : v)) + '\n')
}
