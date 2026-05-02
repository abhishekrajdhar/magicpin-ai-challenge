from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="Vera Challenge Bot", version="1.0.0")
STARTED_AT = time.time()

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_suppression_keys: set[str] = set()
merchant_inbound_fingerprints: dict[str, dict[str, int]] = {}


class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ack_id(scope: str, context_id: str, version: int) -> str:
    raw = f"{scope}:{context_id}:{version}".encode("utf-8")
    return "ack_" + hashlib.sha1(raw).hexdigest()[:12]


def get_payload(scope: str, context_id: str | None) -> dict[str, Any] | None:
    if not context_id:
        return None
    stored = contexts.get((scope, context_id))
    return stored["payload"] if stored else None


def get_in(obj: dict[str, Any] | None, path: str, default: Any = None) -> Any:
    cur: Any = obj or {}
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def pct(value: Any, signed: bool = True) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    sign = "+" if signed and n > 0 else ""
    return f"{sign}{round(n * 100):.0f}%"


def rupee(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value) if str(value).startswith("₹") else f"₹{value}"


def humanize(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).replace("_", " ")


def first_name(merchant: dict[str, Any]) -> str:
    owner = get_in(merchant, "identity.owner_first_name")
    if owner:
        return str(owner).replace("Dr. ", "").strip()
    name = get_in(merchant, "identity.name", "there")
    return str(name).split()[0].replace("Dr.", "Dr.").strip(",")


def salutation(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    cat = category.get("slug", merchant.get("category_slug", ""))
    first = first_name(merchant)
    if cat == "dentists" and not first.lower().startswith("dr"):
        return f"Dr. {first}"
    return first


def language_hint(merchant: dict[str, Any], customer: dict[str, Any] | None = None) -> str:
    pref = get_in(customer, "identity.language_pref", "") if customer else ""
    langs = get_in(merchant, "identity.languages", [])
    combined = " ".join(langs if isinstance(langs, list) else [str(langs)]) + " " + str(pref)
    text = combined.lower()
    if "hi" in text:
        return "hi-en"
    if "te" in text:
        return "te-en"
    if "ta" in text:
        return "ta-en"
    if "kn" in text:
        return "kn-en"
    if "mr" in text:
        return "mr-en"
    return "en"


def active_offer(merchant: dict[str, Any], category: dict[str, Any]) -> str:
    for offer in merchant.get("offers", []):
        if offer.get("status") == "active" and offer.get("title"):
            return offer["title"]
    catalog = category.get("offer_catalog") or []
    return catalog[0]["title"] if catalog else ""


def digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any] | None:
    payload = trigger.get("payload", {})
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    if wanted:
        for item in category.get("digest", []):
            if item.get("id") == wanted:
                return item
    kind = trigger.get("kind", "")
    kind_map = {
        "research_digest": "research",
        "regulation_change": "compliance",
        "cde_opportunity": "cde",
        "category_trend_movement": "trend",
        "supply_alert": "compliance",
    }
    target = kind_map.get(kind)
    for item in category.get("digest", []):
        if item.get("kind") == target:
            return item
    return (category.get("digest") or [None])[0]


def customer_has_consent(customer: dict[str, Any] | None, trigger: dict[str, Any]) -> bool:
    if not customer:
        return False
    consent = customer.get("consent") or {}
    scopes = consent.get("scope") or []
    if not consent.get("opted_in_at") and not scopes:
        return False
    if get_in(customer, "preferences.reminder_opt_in", False):
        return True
    if trigger.get("kind") in {"recall_due", "appointment_tomorrow"}:
        return any(s in scopes for s in ["recall_reminders", "appointment_reminders", "treatment_followup"])
    return bool(get_in(customer, "preferences.reminder_opt_in", True) or scopes)


def metric_line(merchant: dict[str, Any]) -> str:
    perf = merchant.get("performance") or {}
    bits = []
    if perf.get("views") is not None:
        bits.append(f"{perf['views']} views")
    if perf.get("calls") is not None:
        bits.append(f"{perf['calls']} calls")
    if perf.get("directions") is not None:
        bits.append(f"{perf['directions']} direction taps")
    if perf.get("ctr") is not None:
        bits.append(f"CTR {pct(perf['ctr'], signed=False)}")
    return ", ".join(bits[:4])


def cta_for(trigger: dict[str, Any], info_only: bool = False) -> str:
    if info_only:
        return "none"
    if trigger.get("scope") == "customer":
        return "open_ended"
    if trigger.get("urgency", 1) >= 3:
        return "YES/STOP"
    return "open_ended"


def template_name_for(trigger: dict[str, Any], send_as: str) -> str:
    prefix = "merchant" if send_as == "merchant_on_behalf" else "vera"
    kind = re.sub(r"[^a-z0-9_]+", "_", trigger.get("kind", "generic").lower())
    return f"{prefix}_{kind}_v1"


def template_params_for(message: dict[str, Any], merchant: dict[str, Any]) -> list[str]:
    return [
        get_in(merchant, "identity.name", ""),
        message.get("body", "")[:120],
        message.get("cta", "open_ended"),
    ]


def compose_customer(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any]) -> dict[str, Any]:
    cname = get_in(customer, "identity.name", "there")
    mname = get_in(merchant, "identity.name", "the clinic")
    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {})
    offer = active_offer(merchant, category)
    lang = language_hint(merchant, customer)
    slots = payload.get("available_slots") or payload.get("next_session_options") or []
    slot_text = " ya ".join(s.get("label", "") for s in slots[:2] if s.get("label"))
    services = ", ".join((get_in(customer, "relationship.services_received", []) or [])[:2])

    if not customer_has_consent(customer, trigger):
        body = ""
        rationale = "Skipped customer outreach because consent or opt-in scope is missing."
        return {"body": body, "cta": "none", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": rationale}

    if kind == "recall_due":
        last = payload.get("last_service_date") or get_in(customer, "relationship.last_visit")
        due = payload.get("due_date")
        due_text = f" is due around {due}" if due else " is due now"
        slot_part = f" Apke liye slots: {slot_text}." if slot_text else ""
        offer_part = f" {offer} available hai." if offer else ""
        body = f"Hi {cname}, {mname} here. Last visit {last}; your {humanize(payload.get('service_due'), 'follow-up')}{due_text}.{slot_part}{offer_part} Reply with a time that works."
    elif kind in {"appointment_tomorrow", "trial_followup"}:
        slot_part = f" Next option: {slot_text}." if slot_text else ""
        body = f"Hi {cname}, {mname} here. Quick follow-up from your {payload.get('trial_date') or get_in(customer, 'relationship.last_visit', 'last visit')}.{slot_part} Reply YES to confirm, or share another time."
    elif kind in {"chronic_refill_due"}:
        meds = ", ".join(payload.get("molecule_list", [])[:3]) or "your regular medicines"
        runs_out = payload.get("stock_runs_out_iso", "")[:10]
        due_text = f" {runs_out}" if runs_out else ""
        delivery = "delivery address saved hai" if payload.get("delivery_address_saved") else "pickup/delivery dono possible"
        body = f"Namaste {cname}, {mname} se. {meds} refill{due_text} ke around due hai; {delivery}. Reply YES and we will keep it ready."
    else:
        focus = payload.get("previous_focus") or payload.get("next_step_window_open") or services or "your last visit"
        offer_part = f" {offer} bhi active hai." if offer else ""
        body = f"Hi {cname}, {mname} here. Following up on {focus}; thought this may be useful now.{offer_part} Reply YES if you want details."

    if lang in {"hi-en", "te-en", "ta-en", "kn-en", "mr-en"} and "Reply" in body:
        body = body.replace("Reply", "Bas reply")
    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": f"Customer-facing {kind} message using consent, relationship state, slots, and merchant offer without medical guarantees.",
    }


def compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic challenge composer. Inputs and output match challenge-brief.md."""
    if customer:
        return compose_customer(category, merchant, trigger, customer)

    kind = trigger.get("kind", "")
    payload = trigger.get("payload", {})
    name = salutation(category, merchant)
    business = get_in(merchant, "identity.name", "your business")
    locality = get_in(merchant, "identity.locality", "your area")
    category_slug = category.get("slug", merchant.get("category_slug", ""))
    offer = active_offer(merchant, category)
    metrics = metric_line(merchant)
    peer_ctr = get_in(category, "peer_stats.avg_ctr")
    signals = merchant.get("signals", [])
    lang = language_hint(merchant)
    hi = lang == "hi-en"

    body = ""
    rationale = ""
    info_only = False

    if kind in {"research_digest", "regulation_change", "cde_opportunity"}:
        item = digest_item(category, trigger) or {}
        source = item.get("source", "")
        title = item.get("title", "a category update")
        summary = item.get("summary", "")
        trial = f"{item.get('trial_n')}-patient " if item.get("trial_n") else ""
        segment = item.get("patient_segment", "").replace("_", " ")
        merchant_hook = ""
        if "high_risk_adult_cohort" in signals and segment:
            merchant_hook = f" This maps to your {segment} cohort."
        elif peer_ctr and get_in(merchant, "performance.ctr") and get_in(merchant, "performance.ctr") < peer_ctr:
            merchant_hook = f" Your CTR is {pct(get_in(merchant, 'performance.ctr'), False)} vs peer {pct(peer_ctr, False)}."
        deadline = payload.get("deadline_iso", "")[:10]
        deadline_text = f" Deadline: {deadline}." if deadline else ""
        if kind == "cde_opportunity":
            credits = payload.get("credits") or item.get("credits")
            fee = humanize(payload.get("fee") or item.get("actionable", ""))
            body = f"{name}, {title} is on the IDA calendar. {credits} CDE credits; {fee}. Want me to send the 2-line registration note?"
        elif kind == "regulation_change":
            body = f"{name}, important compliance note: {title}. {summary[:125]}{deadline_text} Want me to draft a simple SOP checklist for {business}?"
        else:
            body = f"{name}, {source} has one useful item: {trial}{title}.{merchant_hook} Worth a 2-min look? I can pull it and draft a patient WhatsApp."
        rationale = f"Uses the pushed digest item/source for {category_slug}, ties it to merchant signals, and asks for a low-friction next step."

    elif kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = payload.get("metric", "calls")
        delta = pct(payload.get("delta_pct")) or pct(get_in(merchant, f"performance.delta_7d.{metric}_pct")) or "down"
        baseline = payload.get("vs_baseline")
        season = payload.get("season_note", "").replace("_", " ")
        context = f" vs usual {baseline}" if baseline else ""
        season_text = f" This may be seasonal ({season}), so don't overcorrect." if payload.get("is_expected_seasonal") else ""
        body = f"{name}, quick dashboard flag: {metric} is {delta} over {payload.get('window', '7d')}{context}. {metrics}.{season_text} Want me to draft one Google post around {offer or 'your strongest service'} to recover demand?"
        rationale = "Internal performance dip with exact metric, delta, baseline, and a concrete recovery action."

    elif kind == "perf_spike":
        metric = payload.get("metric", "views")
        driver = payload.get("likely_driver")
        driver_text = f" Likely driver: {driver.replace('_', ' ')}." if driver else ""
        delta = pct(payload.get("delta_pct")) or pct(get_in(merchant, f"performance.delta_7d.{metric}_pct")) or "up"
        body = f"{name}, nice spike: {metric} is {delta} in {payload.get('window', '7d')}; {metrics}.{driver_text} Want me to turn this into a repeatable post/offer for this week?"
        rationale = "Positive internal trigger that names the metric and converts momentum into an action."

    elif kind == "renewal_due":
        days = payload.get("days_remaining") or get_in(merchant, "subscription.days_remaining")
        amount = rupee(payload.get("renewal_amount"))
        body = f"{name}, Pro renewal is due in {days} days{f' ({amount})' if amount else ''}. Current 30-day result: {metrics}. Want me to prepare a renewal summary with wins + pending fixes before you decide?"
        rationale = "Renewal trigger anchored in days remaining, amount when present, and recent performance."

    elif kind in {"winback_eligible", "dormant_with_vera"}:
        days = payload.get("days_since_expiry") or payload.get("days_since_last_merchant_message") or get_in(merchant, "subscription.days_since_expiry")
        lapsed = payload.get("lapsed_customers_added_since_expiry") or get_in(merchant, "customer_aggregate.lapsed_90d_plus") or get_in(merchant, "customer_aggregate.lapsed_180d_plus")
        day_text = f"{days} days" if days is not None else "a while"
        lapsed_text = f"{lapsed} lapsed customers" if lapsed is not None else "your lapsed-customer pool"
        body = f"{name}, it has been {day_text} since the last active Vera loop. I spotted {lapsed_text} and {metrics}. Should I make a 3-message winback draft using {offer or 'a service-price offer'}?"
        rationale = "Dormancy/winback message uses elapsed time, lapsed customer count, and effort externalization."

    elif kind == "review_theme_emerged":
        theme = payload.get("theme", "").replace("_", " ")
        quote = payload.get("common_quote", "")
        count = payload.get("occurrences_30d")
        body = f"{name}, review pattern worth catching: {count} recent reviews mention {theme}. One phrase: \"{quote}\". Want me to draft a reply + a small ops note for the team?"
        rationale = "Review trigger cites the exact theme, count, and customer phrase from context."

    elif kind == "milestone_reached":
        value_now = payload.get("value_now")
        milestone = payload.get("milestone_value")
        metric = humanize(payload.get("metric"), "reviews")
        if value_now is not None and milestone is not None:
            body = f"{name}, {business} is at {value_now} {metric} - almost {milestone}. Want me to draft a thank-you post and a polite review ask to cross it this week?"
        else:
            body = f"{name}, {business} has a {metric} milestone window active. Current snapshot: {metrics}. Want me to draft a thank-you post and a polite review ask?"
        rationale = "Milestone trigger turns an imminent metric into a social-proof action."

    elif kind in {"competitor_opened"}:
        comp = payload.get("competitor_name")
        dist = payload.get("distance_km")
        their = payload.get("their_offer")
        date = payload.get("opened_date")
        if comp and dist and their and date:
            body = f"{name}, {comp} opened {dist} km from {locality} on {date}. Their visible hook is {their}. Want me to draft a sharper {offer or category_slug.rstrip('s')} post so you don't compete only on price?"
        else:
            body = f"{name}, a competitor signal is active near {locality}. Your current hook is {offer or category_slug.rstrip('s')}; want me to draft a sharper Google post so you don't compete only on price?"
        rationale = "Competitor trigger uses only provided competitor details and suggests a defensible counter-position."

    elif kind in {"festival_upcoming", "ipl_match_today", "category_seasonal"}:
        if kind == "ipl_match_today":
            event = f"{payload.get('match')} at {payload.get('match_time_iso', '')[11:16]}"
            body = f"{name}, {event} is today in {payload.get('city', get_in(merchant, 'identity.city'))}. You already have {offer or 'an active offer'}; want me to make a match-night Google post for nearby searches?"
        elif kind == "category_seasonal":
            trends = ", ".join(payload.get("trends", [])[:3]).replace("_", " ")
            body = f"{name}, summer demand shifted: {trends}. {metrics}. Want me to make a 7-day shelf/post checklist for {business}?"
        else:
            festival = payload.get("festival")
            days = payload.get("days_until")
            event_text = f"{festival} is {days} days away" if festival and days is not None else "a seasonal demand window is open"
            body = f"{name}, {event_text}. For {category_slug}, {offer or 'service-price bundles'} will beat generic discounts. Want me to draft one WhatsApp + GBP post?"
        rationale = "External timing trigger linked to category-appropriate commercial action."

    elif kind in {"active_planning_intent"}:
        topic = payload.get("intent_topic", "the plan").replace("_", " ")
        last = payload.get("merchant_last_message", "")
        body = f"{name}, picking up from your message: \"{last}\". Here is the next step for {topic}: package name, price anchor, and one post. Reply YES and I will format the full draft."
        rationale = "Honors explicit merchant intent and moves directly into action."

    elif kind in {"gbp_unverified"}:
        uplift = pct(payload.get("estimated_uplift_pct"), signed=False)
        path = payload.get("verification_path", "verification").replace("_", " ")
        body = f"{name}, your Google profile is still unverified. Verification via {path} can unlock roughly {uplift} more actions. Want me to send the exact 5-minute verification steps?"
        rationale = "GBP trigger uses verification path and estimated uplift from payload."

    elif kind in {"supply_alert"}:
        batches = ", ".join(payload.get("affected_batches", [])[:3])
        body = f"{name}, urgent stock alert: {payload.get('molecule')} batches {batches} from {payload.get('manufacturer')} are flagged. Want me to draft a counter-check note for your staff before dispensing?"
        rationale = "High-urgency pharmacy supply alert with exact molecule, batches, and manufacturer."

    elif kind in {"curious_ask_due"}:
        body = f"{name}, quick operator question: in {locality}, which service is people asking for most this week - {offer or 'your main service'} or something else? I will turn your answer into one post."
        rationale = "Curiosity-driven ask designed to elicit merchant input and then do the work."

    else:
        placeholder = payload.get("metric_or_topic") or kind.replace("_", " ")
        body = f"{name}, quick Vera note for {business}: {placeholder} is active right now. Your latest snapshot: {metrics}. Want me to draft the next WhatsApp/Google post using {offer or 'your best service'}?"
        rationale = "Generic fallback still anchors on trigger kind, merchant metrics, and a concrete next action."

    return {
        "body": body.strip(),
        "cta": cta_for(trigger, info_only),
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": rationale,
    }


@app.get("/v1/healthz")
async def healthz() -> dict[str, Any]:
    counts = {scope: 0 for scope in VALID_SCOPES}
    for scope, _ in contexts:
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - STARTED_AT), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata() -> dict[str, Any]:
    return {
        "team_name": "Vera Deterministic Composer",
        "team_members": ["Abhishek Rajdhar Dubey"],
        "model": "deterministic Python rules + context retrieval",
        "approach": "trigger router with category digest retrieval, merchant/customer personalization, duplicate suppression, and replay intent routing",
        "contact_email": "not-provided@example.com",
        "version": "1.0.0",
        "submitted_at": "2026-05-02T00:00:00Z",
    }


@app.post("/v1/context")
async def push_context(body: ContextPush) -> dict[str, Any]:
    if body.scope not in VALID_SCOPES:
        raise HTTPException(status_code=400, detail={"accepted": False, "reason": "invalid_scope", "details": body.scope})
    key = (body.scope, body.context_id)
    current = contexts.get(key)
    if current and current["version"] > body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
    if current and current["version"] == body.version:
        return {"accepted": True, "ack_id": ack_id(body.scope, body.context_id, body.version), "stored_at": current["stored_at"]}
    contexts[key] = {"version": body.version, "payload": body.payload, "stored_at": now_iso()}
    return {"accepted": True, "ack_id": ack_id(body.scope, body.context_id, body.version), "stored_at": contexts[key]["stored_at"]}


@app.post("/v1/tick")
async def tick(body: TickBody) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for trigger_id in body.available_triggers[:40]:
        trigger = get_payload("trigger", trigger_id)
        if not trigger:
            continue
        suppression = trigger.get("suppression_key") or trigger_id
        if suppression in sent_suppression_keys:
            continue
        merchant = get_payload("merchant", trigger.get("merchant_id"))
        if not merchant:
            continue
        category = get_payload("category", merchant.get("category_slug") or get_in(trigger, "payload.category"))
        if not category:
            continue
        customer = get_payload("customer", trigger.get("customer_id")) if trigger.get("customer_id") else None
        if trigger.get("scope") == "customer" and not customer:
            continue
        message = compose(category, merchant, trigger, customer)
        if not message.get("body"):
            sent_suppression_keys.add(suppression)
            continue
        conv_id = f"conv_{trigger.get('merchant_id')}_{trigger_id}_{len(conversations) + 1}"
        action = {
            "conversation_id": conv_id,
            "merchant_id": trigger.get("merchant_id"),
            "customer_id": trigger.get("customer_id"),
            "send_as": message["send_as"],
            "trigger_id": trigger_id,
            "template_name": template_name_for(trigger, message["send_as"]),
            "template_params": template_params_for(message, merchant),
            **message,
        }
        conversations[conv_id] = {"merchant_id": trigger.get("merchant_id"), "customer_id": trigger.get("customer_id"), "trigger_id": trigger_id, "turns": [{"from": "bot", "body": message["body"], "ts": body.now}]}
        sent_suppression_keys.add(suppression)
        actions.append(action)
        if len(actions) >= 20:
            break
    return {"actions": actions}


AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "will respond shortly",
    "automated assistant",
    "away right now",
    "business account",
    "team tak pahuncha",
]


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9₹% ]+", " ", text.lower())).strip()


def looks_like_auto_reply(message: str, merchant_id: str | None) -> bool:
    norm = normalized(message)
    if any(p in norm for p in AUTO_REPLY_PATTERNS):
        return True
    if not merchant_id:
        return False
    seen = merchant_inbound_fingerprints.setdefault(merchant_id, {})
    seen[norm] = seen.get(norm, 0) + 1
    return seen[norm] >= 2 and len(norm) > 25


def is_stop(message: str) -> bool:
    text = normalized(message)
    return any(w in text for w in ["stop", "unsubscribe", "not interested", "useless spam", "spam", "mat bhejo", "band karo"])


def is_commitment(message: str) -> bool:
    text = normalized(message)
    return any(w in text for w in ["yes", "ok", "okay", "go ahead", "lets do", "let s do", "do it", "proceed", "send", "start", "join", "judrna", "judna", "kar do", "chalu"])


@app.post("/v1/reply")
async def reply(body: ReplyBody) -> dict[str, Any]:
    conv = conversations.setdefault(body.conversation_id, {"merchant_id": body.merchant_id, "customer_id": body.customer_id, "turns": []})
    conv["turns"].append({"from": body.from_role, "body": body.message, "ts": body.received_at})

    if is_stop(body.message):
        return {"action": "end", "rationale": "Merchant/customer opted out or hostile; ending immediately."}

    if looks_like_auto_reply(body.message, body.merchant_id):
        return {"action": "end", "rationale": "Detected WhatsApp Business auto-reply/canned response; stopped to avoid polluted turns."}

    merchant = get_payload("merchant", body.merchant_id) or {}
    merchant_name = get_in(merchant, "identity.name", "your business")

    if is_commitment(body.message):
        response = f"Done, moving to action. I will prepare the draft/update for {merchant_name} now; next I need only one confirmation on the final text before sending."
        conv["turns"].append({"from": "bot", "body": response, "ts": now_iso()})
        return {"action": "send", "body": response, "cta": "open_ended", "rationale": "Detected explicit commitment and switched from pitch/qualification to action mode."}

    if "price" in normalized(body.message) or "cost" in normalized(body.message) or "kitna" in normalized(body.message):
        response = "Fair question. I will keep it service-price specific and use only your active offer/catalog price, not a generic discount. Share YES and I will draft the exact version."
    elif "later" in normalized(body.message) or "busy" in normalized(body.message):
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Merchant asked for time or signaled they are busy; backing off for 30 minutes."}
    else:
        response = f"Got it. I will keep this practical for {merchant_name}: one short draft, one clear CTA, and no extra questions. Reply YES if I should proceed."
    conv["turns"].append({"from": "bot", "body": response, "ts": now_iso()})
    return {"action": "send", "body": response, "cta": "YES/STOP", "rationale": "Acknowledged reply and advanced with one low-friction next step."}


@app.post("/v1/teardown")
async def teardown() -> dict[str, Any]:
    contexts.clear()
    conversations.clear()
    sent_suppression_keys.clear()
    merchant_inbound_fingerprints.clear()
    return {"accepted": True, "wiped_at": now_iso()}
