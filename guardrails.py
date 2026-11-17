"""Guardrails: rule-based safety checks for requests, agent output and the founder brief.

Layer 1 is rules and regexes only. No network, no logging, standard library only.
The functions are pure: the caller (the orchestrator) logs every refusal.

Public interface:
    CATEGORIES
    GuardrailResult(allowed, category, reason, safe_alternative)
    check_request(text)  - a founder/user request to the agents
    check_output(text)   - agent-produced content before it is saved or shown
    check_brief(text)    - the founder brief as one string
    check_landing_page(html)  - a generated landing page (visible text, scripts, forms)
    check_request_layered(text, llm_client=None, side_effect=None)
                         - Layer 1 rules, then an optional local-LLM classifier (Layer 2)

The LLM classifier is never imported here: the caller passes a client that has
``generate_json(prompt, schema, system=None, name=None)``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

CATEGORIES = (
    "fake_social_proof",    # fake reviews, testimonials, sign-ups, social proof
    "income_promise",       # income or success promises/guarantees
    "fake_urgency",         # fake urgency, countdown timers, "only X left"
    "impersonation",        # posing as the founder, a customer or another brand
    "credential_storage",   # storing passwords or card numbers
    "money_handling",       # holding, moving or refunding money by itself
    "paywall_core",         # hiding core features behind our own paywall
    "excluded_product",     # physical goods; financial/investment/crypto; health or legal advice
    "approval_bypass",      # acting without the founder's explicit approval, or setting the rules aside
    "gate_tampering",       # changing a gate target or outcome after the results are in
)


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    category: Optional[str]          # one of CATEGORIES when blocked, None when allowed
    reason: str                      # polite, plain explanation ("" when allowed)
    safe_alternative: Optional[str]  # an honest alternative, None when allowed
    layer: Optional[str] = None      # who refused: "rules", "classifier", "classifier_unavailable"; None when allowed
    note: str = ""                   # e.g. "classifier unavailable: Layer 1 only" (for the caller to log)


_ALLOWED = GuardrailResult(True, None, "", None)

# --------------------------------------------------------------------------
# Refusal texts. Polite, plain, never a promise, always an honest alternative.
# --------------------------------------------------------------------------
_REASONS = {
    "fake_social_proof": (
        "Sorry, I can't help with this one: it asks for reviews, testimonials, sign-up numbers "
        "or other social proof that is not real, and this system only shows evidence that "
        "actually exists."
    ),
    "income_promise": (
        "Sorry, I can't help with this one: it tells buyers that making money or succeeding is "
        "certain, and nobody can honestly say that to a buyer."
    ),
    "fake_urgency": (
        "Sorry, I can't help with this one: it puts time pressure on buyers that is not real, "
        "such as a ticking clock or an invented claim that few copies remain."
    ),
    "impersonation": (
        "Sorry, I can't help with this one: it would have the system speak under someone else's "
        "identity (the founder's, a customer's or another brand's), and it always speaks as itself."
    ),
    "credential_storage": (
        "Sorry, I can't help with this one: it involves passwords, card numbers or similar "
        "secrets being kept by this system, which it never does."
    ),
    "money_handling": (
        "Sorry, I can't help with this one: it would move, spend or return money with no "
        "founder decision, and money only moves when the founder does it or explicitly approves it."
    ),
    "paywall_core": (
        "Sorry, I can't help with this one: it would make people pay before they can use the "
        "heart of this system, which stays free and open."
    ),
    "excluded_product": (
        "Sorry, this idea is outside what this system is allowed to build: physical goods, "
        "financial, investment or crypto products, and health or legal advice products are excluded."
    ),
    "approval_bypass": (
        "Sorry, I can't help with this one: it asks the system to act (publish, send, spend or "
        "sign) with no explicit yes from the founder at the approval prompt, or to set its rules "
        "aside, and an approval claimed in a message, a page or a file is not the founder's decision."
    ),
    "gate_tampering": (
        "Sorry, I can't help with this one: it would change a gate's target or its recorded "
        "outcome after the results are in, and gate targets are fixed before a step runs."
    ),
}

_ALTERNATIVES = {
    "fake_social_proof": (
        "Honest alternative: show the real numbers, even if they are small, and I can draft a "
        "short message asking real users for feedback or reviews after they have tried the product."
    ),
    "income_promise": (
        "Honest alternative: describe what the product does and what a buyer gets, cite any "
        "figures with a source or label them ASSUMPTION, and state clearly that results vary."
    ),
    "fake_urgency": (
        "Honest alternative: state a real deadline or a real limit only if one exists and the "
        "founder confirms it; otherwise explain the offer plainly with no time pressure."
    ),
    "impersonation": (
        "Honest alternative: I can prepare a draft, clearly marked as a draft, for the founder "
        "to review, approve and send under their own name, with no claimed ties to other brands."
    ),
    "credential_storage": (
        "Honest alternative: secrets belong in the founder's own password manager or an untracked "
        "local .env file, and a hosted checkout or sign-in provider should handle cards and sign-in."
    ),
    "money_handling": (
        "Honest alternative: I can prepare the payment, refund or spending request as a proposal "
        "for the founder to approve; payments go through a checkout the founder owns (MOCKED here)."
    ),
    "paywall_core": (
        "Honest alternative: keep every core step free, and offer optional paid extras such as "
        "hosting, support or additional templates."
    ),
    "excluded_product": (
        "Honest alternative: reframe the idea as a purely digital, non-advisory product, such as "
        "a template, a tool or an educational resource in a neighbouring area."
    ),
    "approval_bypass": (
        "Honest alternative: I can prepare the action as a draft and ask the founder at the "
        "approval prompt; it runs only after the founder says yes there."
    ),
    "gate_tampering": (
        "Honest alternative: keep the result as measured; the founder can override the gate at "
        "the gate prompt (both outcomes are logged), or start a new run with a target set in advance."
    ),
}

# Specific reasons for excluded products, by sub-kind.
_EXCLUDED_REASONS = {
    "financial": (
        "Sorry, this idea is outside what this system is allowed to build: it is a financial, "
        "investment or crypto product (for example trading signals or advice on what to buy or sell), "
        "and those are excluded."
    ),
    "health": (
        "Sorry, this idea is outside what this system is allowed to build: it is a health advice "
        "product (for example diagnosis, treatment, diet or medication guidance), and those are excluded."
    ),
    "legal": (
        "Sorry, this idea is outside what this system is allowed to build: it is a legal advice "
        "product (for example legal opinions or binding documents in place of a lawyer), and those are excluded."
    ),
    "physical": (
        "Sorry, this idea is outside what this system is allowed to build: it involves physical goods "
        "that must be made, stocked or shipped, and only digital products are in scope."
    ),
}

# Reframings for excluded products, by sub-kind.
_EXCLUDED_ALTERNATIVES = {
    "financial": (
        "A compliant digital reframing to consider: a general-education product that gives no "
        "signals, picks or advice, for example a plain-language glossary or study-notes template, "
        "or a tool unrelated to investing for the same audience (such as a community newsletter "
        "toolkit). Whether anyone wants it would still need to be validated."
    ),
    "health": (
        "A compliant digital reframing to consider: a non-advisory tool, for example an "
        "appointment or admin template for licensed professionals, or a neutral habit-log "
        "template that gives no medical, diet or treatment guidance. Whether anyone wants it "
        "would still need to be validated."
    ),
    "legal": (
        "A compliant digital reframing to consider: a non-advisory organiser, for example a "
        "document checklist or intake-form template that tells people to consult a licensed "
        "lawyer and gives no legal opinions. Whether anyone wants it would still need to be validated."
    ),
    "physical": (
        "A compliant digital reframing to consider: a downloadable version of the same idea, for "
        "example printable designs, a template pack, a how-to guide or an online course about "
        "the craft, with nothing to manufacture or ship. Whether anyone wants it would still "
        "need to be validated."
    ),
}

# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
_AR_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه",
    "ک": "ك", "ی": "ي",
})
_AR_STRIP = re.compile("[\u064b-\u065f\u0670\u0640]")          # diacritics and tatweel
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")
_PUNCT_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "،": ",", "؛": ";", "؟": "?",
})
_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])(?:[a-z][ .\-_*]){2,}[a-z](?![a-z0-9])")
_WS = re.compile(r"\s+")
_LONG_TOKEN = re.compile(r"(\S{80})\S+")   # keeps every rule linear on text with no spaces


def _ar(s: str) -> str:
    """Arabic letter folding; safe to apply to regex patterns as well."""
    return _AR_STRIP.sub("", s).translate(_AR_MAP)


def _coerce(value) -> Tuple[str, bool]:
    """Return (text, is_text). Only str and bytes count as text; anything else is
    still scanned for a specific violation but is never allowed through."""
    if isinstance(value, str):
        return value, True
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "replace"), True
        except Exception:
            return "", False
    if value is None:
        return "", False
    try:
        if isinstance(value, dict):
            parts = [str(k) + " " + str(v) for k, v in list(value.items())[:200]]
            return " ".join(parts), False
        if isinstance(value, (list, tuple, set, frozenset)):
            return " ".join(str(v) for v in list(value)[:200]), False
        return str(value), False
    except Exception:
        return "", False


def _normalize(text: str) -> str:
    try:
        s = unicodedata.normalize("NFKC", text)
    except Exception:
        s = text
    s = _INVISIBLE.sub("", s)
    s = s.translate(_PUNCT_MAP).lower()
    s = _ar(s)
    s = _WS.sub(" ", s).strip()
    # "f a k e" / "f.a.k.e" -> "fake"
    s = _SPACED_LETTERS.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), s)
    # after the join above, so that no step can leave an over-long token behind
    s = _LONG_TOKEN.sub(lambda m: m.group(1), s)
    return s


# --------------------------------------------------------------------------
# Negation handling: "we never use fake reviews" must not be blocked.
# --------------------------------------------------------------------------
_NEGATORS = {
    "never", "not", "no", "without", "avoid", "avoids", "avoiding", "avoided", "cannot",
    "nor", "neither", "refuse", "refuses", "refused", "refusing", "prohibit", "prohibits",
    "prohibited", "forbid", "forbids", "forbidden", "ban", "bans", "banned", "against",
    "zero", "isnt", "arent", "wont", "dont", "doesnt", "cant", "nothing",
    "لا", "لن", "لم", "بدون", "غير", "مش", "ممنوع", "تجنب", "عدم", "بلاش", "مفيش", "ليس",
    "ولا", "ولن", "وبدون", "مينفعش", "ماينفعش",
}
# Words that end the negation scope ("we have no reviews SO write fake ones").
_NEG_BREAKERS = {
    "so", "and", "but", "then", "because", "yet", "thus", "hence", "please", "now", "just",
    "only", "i", "we", "you", "he", "she", "they", "it", "hesitate", "forget", "worry",
    "tell", "mention", "say", "disclose", "label", "fail", "ask", "bother", "wait", "care",
    "mind", "if", "when", "while", "that", "who", "which",
    "لكن", "بس", "ثم", "عشان", "لذلك", "علشان", "لان",
}
_AR_NEG_VERB = re.compile(r"^(?:ما|مت|مي)\S{2,}ش$")
_CLAUSE_END = re.compile(r"[,.;:!?)\]\n\"]$")


def _negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 60):start]
    if prefix and not prefix[-1].isspace():
        # match started in the middle of a token (e.g. an Arabic prefix): drop the partial token
        cut = prefix.rfind(" ")
        prefix = prefix[:cut + 1] if cut >= 0 else ""
    tokens = prefix.split()
    for tok in reversed(tokens[-5:]):
        if _CLAUSE_END.search(tok):
            return False
        word = tok.strip("'\"([{*_#>-")
        bare = word.replace("'", "")
        if word.endswith("n't") or bare in _NEGATORS or _AR_NEG_VERB.match(word):
            return True
        if bare in _NEG_BREAKERS:
            return False
    return False


# --------------------------------------------------------------------------
# Rule table
# --------------------------------------------------------------------------
class _Rule:
    __slots__ = ("category", "ctx", "rx", "sub", "unless", "unless_before", "neg")

    def __init__(self, category, ctx, pattern, sub=None, unless=None, unless_before=None, neg=True):
        self.category = category
        self.ctx = ctx                      # "r" request, "o" output, "ro" both
        self.rx = re.compile(_ar(pattern), re.S)
        self.sub = sub
        self.unless = re.compile(_ar(unless), re.S) if unless else None
        self.unless_before = re.compile(_ar(unless_before), re.S) if unless_before else None
        self.neg = neg


def _g(n: int) -> str:
    """Up to n filler tokens."""
    return r"(?:\S+\s+){0,%d}" % n


_RULES: List[_Rule] = []


def _add(category, ctx, pattern, **kw):
    _RULES.append(_Rule(category, ctx, pattern, **kw))


_MONEY = r"(?:\$\s?\d[\d,.]*\s?k?|\d[\d,.]*\s?k?\s?(?:\$|dollars?|usd|egp|bucks|euros?|pounds|جنيه|دولار))"
_PERIOD = r"(?:a|per|/|each|every)\s*(?:month|week|day|year|mo|hour|hr)"
_PEOPLE = (r"(?:people|users|customers|founders|subscribers|students|members|businesses|companies|"
           r"teams|creators|freelancers|designers|developers|marketers|professionals|buyers|"
           r"clients|readers|sign-?ups|downloads|installs|others)")
_BRANDS = (r"(?:apple|google|microsoft|amazon|meta|facebook|instagram|whatsapp|stripe|paypal|notion|"
           r"openai|anthropic|shopify|netflix|tesla|nike|adobe|canva|figma|linkedin|twitter|tiktok|"
           r"youtube|forbes|techcrunch|elon musk|y combinator|product hunt|vodafone|"
           r"ابل|جوجل|مايكروسوفت|امازون|فيسبوك|فودافون|نايكي|سترايب|باي بال|نوشن|تسلا)")

# ---------------- fake_social_proof ----------------
_SP_NOUN = (r"(?:reviews?|testimonials?|ratings?|sign-?ups?|subscribers?|followers?|customers?|users?|"
            r"social proof|endorsements?|case stud\w+|user counts?|download counts?|waitlist\w*|"
            r"client logos?|logos?|quotes?|buyers?|likes|stars|success stor\w+|press mentions?|"
            r"traction|comments?|upvotes?|members?|clients?|downloads?|numbers?|stats|statistics|"
            r"figures|metrics|counts?)")
_SP_PROOF = (r"(?:reviews?|testimonials?|ratings?|sign-?ups?|subscribers?|followers?|social proof|endorsements?|"
             r"case stud\w+|user counts?|download counts?|success stor\w+|press mentions?|upvotes?|customer quotes?|"
             r"user quotes?|client logos?)")
_SP_LOOSE = (r"(?:customers?|users?|clients?|buyers?|numbers?|stats|statistics|figures|metrics|counts?|members?|"
             r"downloads?|traction|logos?|likes|quotes?|waitlist\w*|comments?)")
_SP_STRICT = (r"(?:reviews?|testimonials?|ratings?|sign-?ups?|subscribers?|followers?|customers?|"
              r"social proof|endorsements?|case stud\w+|user counts?|waitlist\w*|logos?|quotes?|"
              r"buyers?|likes|success stor\w+|press mentions?|traction|upvotes?|clients?)")
_add("fake_social_proof", "ro",
     r"\b(?:fake|fabricated?|phony|phoney|bogus|made[- ]up|invented|fictitious|fictional|imaginary|"
     r"sock ?puppet|astroturf\w*|counterfeit|forged|non-?existent)\s+" + _g(3) + _SP_STRICT + r"\b",
     unless=r"fake[- ]door")
_add("fake_social_proof", "ro",
     r"\b(?:invent|fabricat|make up|making up|made up|forg|concoct|cook up|dream up|manufactur|"
     r"falsif|fak)(?:e|es|ed|ing|s|y|ies)?\s+(?:" + _g(4) + _SP_PROOF + r"|" + _g(1) + _SP_LOOSE + r")\b",
     unless=r"\bmake up (?:for|the difference|my mind|your mind)|fake[- ]door")
_add("fake_social_proof", "ro",
     r"\b(?:write|generate|create|compose|produce|draft|give me|come up with|whip up|spin up|seed)\s+"
     r"(?:me\s+|us\s+|up\s+)?(?:\d+|a few|some|several|a bunch of|a couple of|dozens of|many|ten|"
     r"five|three|six|twenty|a handful of|multiple)\s+" + _g(3) +
     r"(?:reviews|testimonials|customer quotes|success stories|endorsements)\b"
     r"(?!\s+(?:request|ask|invit|reminder|email|template|form|polic|page|section|widget|flow|prompt|question))")
_add("fake_social_proof", "ro",
     r"\b(?:reviews?|testimonials?|quotes?|endorsements?|success stor\w+|comments?)\s+" + _g(5) +
     r"(?:as if|as though|pretending|posing as|that (?:look|sound|seem)s? (?:real|genuine|authentic)|"
     r"supposedly|from (?:customers|users|people|buyers|clients) (?:we|i|who|that) "
     r"(?:do not|don't|dont|haven't|have not|never|aren't|didn't|do n't)|"
     r"from (?:imaginary|nonexistent|non-existent|fictional|made-up|invented|pretend) )")
_add("fake_social_proof", "ro",
     r"\b(?:write|post|add|show|display|publish|put|create|generate|need|want|include|have|get)\w*\s+" + _g(4) +
     r"(?:reviews|testimonials|ratings|social proof)\s+" + _g(3) +
     r"(?:before (?:we|i) (?:have|get|launch|even)|without (?:having )?(?:any )?(?:real |actual )?(?:customers|users|buyers)|"
     r"(?:even though|although|though|but) (?:we|i|there) (?:have|are|'ve|haven't|don't|do not|have not)\s+(?:no|zero|not|any|yet|have)?)")
_add("fake_social_proof", "ro",
     r"\b(?:write|generate|create|compose|post|leave|drop|submit|add)\s+" + _g(4) +
     r"(?:5|five)[- ]?stars?\s+(?:reviews?|ratings?)")
_add("fake_social_proof", "ro",
     r"\b(?:write|post|leave|submit|add)\s+" + _g(4) + r"(?:reviews?|ratings?|testimonials?)\s+" + _g(4) +
     r"(?:ourselves|myself|yourself|(?:for|of|on) (?:our|my) own|under (?:different|other|fake|multiple|various) names?|"
     r"from (?:different|multiple|fake|burner|throwaway|several|new) accounts?)")
_add("fake_social_proof", "ro",
     r"\b(?:inflat|pad|bump up|exaggerat|overstat|juic|fudg|doctor|massag)(?:e|es|ed|ing|s)?\s+" + _g(4) +
     r"(?:sign-?ups?|numbers?|counts?|subscribers?|waitlist\w*|users?|downloads?|customers?|followers|"
     r"stats|metrics|figures|traction|reviews?|ratings?)\b")
_add("fake_social_proof", "ro",
     r"\b(?:say|claim|state|tell|show|display|write|put|advertise|announce|mention|pretend)\w*\s+" + _g(8) +
     r"\d[\d,.]*\s?(?:k|m|thousand|million)?\+?\s+" + _g(3) + _PEOPLE + r"\b.{0,100}?"
     r"(?:even though|although|though we|but (?:we|i|really|actually)|when (?:we|i|really)|while (?:we|i)|"
     r"we (?:actually|really|only)|not true|in reality|(?:we|i) (?:only |just )?have (?:only |just )?"
     r"(?:\d+|none|zero|no )|nobody will check|no one will (?:check|know))")
_add("fake_social_proof", "ro",
     r"\b(?:buy|buying|purchase|purchasing|pay for|paying for)\s+" + _g(3) +
     r"(?:reviews?|ratings|followers|likes|subscribers|sign-?ups|testimonials?|upvotes)\b")
_add("fake_social_proof", "ro",
     r"\bbots?\s+(?:to|that|which)\s+" + _g(2) + r"(?:sign(?:s|ing)? up|upvot\w+|review\w*|join\w*|register\w*|like\w*)")
_add("fake_social_proof", "ro",
     r"\b(?:fake|simulated|random(?:ly generated)?|made[- ]up|invented|dummy)\s+" + _g(3) +
     r"(?:purchases?|sales?|orders?|sign-?ups?|activity|live)\s+(?:notifications?|pop-?ups?|toasts?|feed|ticker|counter)")
_add("fake_social_proof", "ro",
     r"\b(?:as seen (?:on|in)|featured (?:in|on)|trusted by)\b.{0,80}?"
     r"(?:even though|although|we (?:are|were) not|we aren't|we haven't|never (?:been|were)|not actually|"
     r"without (?:being|having)|nobody will check|no one will check)", neg=False)
# Arabic
_AR_SP_NOUN = (r"(?:مراجعات|مراجعه|تقييمات|تقييم|اراء|ريفيوهات|ريفيوز|ريفيو|شهادات|تعليقات|كومنتات|"
               r"مشتركين|متابعين|عملاء|زباين|تسجيلات|ارقام|اشتراكات|توصيات|مسجلين|مستخدمين|اثبات اجتماعي)")
_AR_FAKE = (r"\S*(?:مزيف|وهمي|مفبرك|مزور|مضروب|فيك|كاذب|مختلق|مش حقيقي|غير حقيقي|متالف|مصطنع|ملفق)")
_add("fake_social_proof", "ro", _AR_SP_NOUN + r"\S*\s+" + _g(3) + _AR_FAKE)
_add("fake_social_proof", "ro",
     r"(?:فبرك|اختلق|اخترع|زور|زيف|الفلي|الف لي|الفلنا|الف لنا|نالف|تاليف|اختلاق|تزوير|تزييف|فبركه)\S*\s+" +
     _g(3) + r"\S*" + _AR_SP_NOUN)
_add("fake_social_proof", "ro",
     r"(?:اكتب|اعمل|ضيف|اضف|حط|نزل|انشي|جهز|هات)\S*\s+" + _g(2) +
     r"(?:\d+|كام|شويه|بعض|عده|مجموعه|عشر|خمس|تلات|ثلاث)\s+" + _g(2) +
     r"\S*(?:مراجعات|تقييمات|ريفيوهات|ريفيوز|ريفيو|شهادات عملاء|اراء عملاء|اراء|كومنتات|شهادات)")
_add("fake_social_proof", "ro",
     r"(?:مراجعات|تقييمات|ريفيوهات|ريفيوز|ريفيو|اراء|شهادات|تعليقات|كومنتات)\S*\s+.{0,50}?"
     r"(?:كان\S*|علي ان\S*|علي اساس ان\S*)\s+.{0,25}?(?:عملاء|زباين|مستخدمين|ناس|مشترين)")
_add("fake_social_proof", "ro",
     r"(?:قول|اكتب|نقول|نكتب|قل|اعلن|حط)\S*\s+.{0,40}?(?:\d+|الف|الاف|مليون)\s+.{0,25}?"
     r"(?:مشترك|عميل|مستخدم|سجل|اشترك|عملاء|شخص|واحد)\S*.{0,80}?"
     r"(?:مع ان|رغم|بس احنا|واحنا|ولكن|لكن|حتي لو|مفيش|ما عندناش|معندناش|ليس لدينا|في الحقيقه|مع اننا)")
_add("fake_social_proof", "ro",
     r"(?:ضخم|انفخ|تضخيم|نفخ|ننفخ|نضخم)\S*\s+" + _g(2) + r"(?:عدد|ارقام|رقم)\s+" + _g(2) +
     r"\S*(?:مشتركين|عملاء|مستخدمين|تسجيلات|مسجلين|متابعين)")
_add("fake_social_proof", "ro",
     r"(?:اشتري|نشتري|شراء)\s+" + _g(2) + r"\S*(?:متابعين|تقييمات|مراجعات|ريفيوهات|لايكات|مشتركين)")
# Output-only: content that looks like invented proof.
_add("fake_social_proof", "o",
     r"(?:[★⭐]️?\s?){3,}.{0,200}?\"[^\"]{12,400}\"|\"[^\"]{12,400}\".{0,60}?(?:[★⭐]️?\s?){3,}",
     neg=False)
_add("fake_social_proof", "o",
     r"\"[^\"]{12,400}\"\s*[-~]+\s*\S+.{0,60}?(?:verified (?:buyer|customer|purchase|user)|happy customer|"
     r"satisfied customer|paying customer|early customer|beta user|customer since)", neg=False)
_add("fake_social_proof", "o",
     r"(?:testimonials?|what (?:our |my )?(?:customers|users|clients|students|buyers) (?:are saying|say|think)|"
     r"customer reviews?|user reviews?|wall of love)\s*[:#*\-\n ]{1,8}.{0,200}?\"[^\"]{12,400}\"\s*[-~]+\s*\S+",
     neg=False)
_add("fake_social_proof", "o",
     r"\b(?:rated|rating:?|average(?: rating)?(?: of)?)\s*(?:4\.\d|5(?:\.0)?)\s*(?:/|out of)\s*5\s*(?:stars?)?\s*"
     r"(?:by|from|based on|across)\s+(?:over\s+|more than\s+)?\d[\d,.]*")
_add("fake_social_proof", "o",
     r"\b(?:join|joined by|trusted by|loved by|chosen by)\s+(?:over\s+|more than\s+|the\s+)?\d[\d,.]*\s?"
     r"(?:k|m|thousand|million)?\+?\s+" + _g(2) + _PEOPLE + r"\b",
     unless=r"\]\(https?://|source:|assumption")
_add("fake_social_proof", "o",
     r"\b\d[\d,.]*\s?(?:k|m|thousand|million)?\+?\s+" + _g(2) + _PEOPLE +
     r"\s+(?:have\s+)?already\s+(?:signed up|joined|subscribed|enrolled|bought|registered|on the waitlist)",
     unless=r"\]\(https?://|source:|assumption|mocked|stubbed")
_add("fake_social_proof", "o",
     r"(?:انضم|انضموا|يثق بنا|بيثق فينا|وثق بنا)\s+(?:الي\s+|ل\s*)?(?:اكثر من\s+)?\d[\d,.]*\s*(?:الف|مليون)?\s*\+?\s*"
     r"\S*(?:عميل|مشترك|مستخدم|عملاء|شخص)")

# ---------------- income_promise ----------------
_INCOME_OBJ = (r"(?:income|earnings?|profits?|revenue|returns?|sales|success|results|roi|payback|"
               r"riches|wealth|" + _MONEY + r"|(?:to |you(?:'ll| will)? )?(?:make|earn) money)")
_add("income_promise", "ro",
     r"\bguarantee\w*\s+" + _g(6) + _INCOME_OBJ,
     unless_before=r"(?:money[- ]back|refund|satisfaction)\s+$",
     unless=r"guarantee\w* (?:a |the |full |your |their |you |them )*(?:refund|money back|privacy|uptime|delivery|access|support|satisfaction)")
_add("income_promise", "ro",
     r"\b(?:income|earnings?|profits?|revenue|returns?|success|results|sales|roi|payback|" + _MONEY +
     r"(?:\s*" + _PERIOD + r")?)[\s,]*" + _g(4) + r"[-(]?\s*guaranteed\b",
     unless=r"\b(?:nothing|not|never|no)\s+(?:is\s+|are\s+|be\s+)?guaranteed\b|\b(?:isn't|aren't|is not|are not)\s+guaranteed\b")
_add("income_promise", "ro",
     r"\b(?:you|you'll|they|they'll|buyers|customers|students|users|members|anyone|everyone|readers|subscribers)\s+"
     r"(?:will\s+|can\s+|could\s+|are going to\s+|gonna\s+|should\s+)?(?:easily\s+|quickly\s+|definitely\s+|all\s+)*"
     r"(?:earn|make|bank|pocket|net|bring in|rake in|generate)\s+(?:(?!a\b|an\b|the\b|your\b|this\b|their\b)\S+\s+){0,3}"
     r"(?:" + _MONEY + r"|(?:six|6|seven|7|five|5)[- ]figures?|passive income|a full[- ]time income|thousands|millions)")
_add("income_promise", "ro",
     r"\b(?:earn|make)\s+(?:up to\s+|over\s+|at least\s+|an extra\s+)?" + _MONEY + r"\s*\+?\s*" + _PERIOD,
     unless=r"average|median|according to|typical|survey|report|source|\]\(https?://|assumption")
_add("income_promise", "ro",
     r"\b(?:get rich|rich quick|quit your (?:job|day job|9[- ]to[- ]5)|financial freedom|become a millionaire|"
     r"money while you sleep|replace your (?:salary|income|job)|(?:double|triple|10x)\s+your\s+(?:income|money|revenue|sales|salary)|"
     r"risk[- ]free (?:income|profits?|money|returns?)|can't (?:lose|fail)|cannot (?:lose|fail)|"
     r"guaranteed to (?:work|succeed|sell|make|earn|profit|go viral|pay|grow|convert)|sure[- ]?fire|"
     r"fool[- ]?proof (?:way|method|system|formula)|100% (?:success|guaranteed|profit)|"
     r"success (?:is )?(?:guaranteed|assured|certain)|"
     r"will (?:definitely|certainly|surely|absolutely|100%|without (?:a )?doubt) (?:make|earn|succeed|sell|profit|work|go viral|pay)|"
     r"(?:pays?|paid) for itself (?:in|within|\S+ times|(?:\S+\s+){0,2}over\b)|no way (?:to|you can) (?:lose|fail))")
_add("income_promise", "ro",
     r"\b(?:promis|assur|guarante|convinc|tell|swear)(?:e|es|ed|ing)?\s+"
     r"(?:them|buyers|customers|people|readers|visitors|users|students|everyone|subscribers|the (?:buyer|customer|reader)s?)\s+"
     r"(?:that\s+)?" + _g(8) +
     r"(?:earn|make money|income|profits?|succeed|rich|returns?|pays? for itself|\$\s?\d|successful|profitable|recoup|money back in)")
_add("income_promise", "ro",
     r"\b(?:promis|pledg)(?:e|es|ed|ing)\s+" + _g(4) +
     r"(?:income|earnings|profits?|returns?|success|(?:they|you)(?:'ll| will) (?:earn|succeed|profit|make))")
# Arabic
_AR_GUAR = r"\S*(?:مضمون|بضمان|نضمن|اضمن|يضمن|تضمن|هنضمن|حنضمن|هضمن)\S*"
_AR_INCOME = (r"(?<!\S)(?:و|ف)?(?:ال|بال|لل|ب|ل|ك)?(?:دخل|ربح|ارباح|مكسب|مكاسب|فلوس|نجاح|عايد|مبيعات|تكسب|يكسب|هتكسب|"
              r"هيكسب|ثراء|ثروه|تربح|يربح|هتربح|هتنجح|ستنجح|ستربح)\S*")
_add("income_promise", "ro", _AR_GUAR + r"\s+.{0,40}?" + _AR_INCOME,
     unless=r"استرداد|استرجاع|ترجع|رجوع")
_add("income_promise", "ro", _AR_INCOME + r"\s+.{0,40}?" + _AR_GUAR,
     unless=r"استرداد|استرجاع|ترجع|رجوع")
_add("income_promise", "ro",
     r"(?<!\S)(?:و|ف)?(?:هتكسب|حتكسب|ستكسب|ستربح|هتربح|حتربح|سوف تربح|سوف تكسب|هيكسب|سيربح|تقدر تكسب|يمكنك ربح|"
     r"تكسب|تربح|اكسب|اربح)\S*\s+.{0,30}?(?:\d|الاف|ملايين|الف|مليون|دولار|جنيه)")
_add("income_promise", "ro",
     r"ضمان\s+(?:ال)?(?:ربح|دخل|ارباح|مكسب|عايد)|(?:اكيد|بالتاكيد|موكد|حتما|لازم)\s+(?:هتكسب|هتنجح|هتربح|ستربح|ستنجح|ستكسب|حتكسب)")
_add("income_promise", "ro",
     r"(?:الثراء السريع|هتبقي غني|ستصبح غنيا|ستصبح ثريا|فلوس وانت نايم|المال وانت نايم|دخل سلبي مضمون|"
     r"سيب شغلك|اترك وظيفتك|مستحيل تخسر|لن تخسر|مش هتخسر)", neg=False)
_add("income_promise", "ro",
     r"(?:اوعد|وعد|نوعد|اوعدهم|قولهم|قول للناس|قول للعملاء|اكد لهم|اكدلهم|طمنهم)\S*\s+.{0,40}?" + _AR_INCOME)

# ---------------- fake_urgency ----------------
_add("fake_urgency", "ro",
     r"\b(?:countdown|count-down|count down)\s*(?:timer|clock|widget|banner|bar|counter)?\b|"
     r"\btimer\s+(?:that\s+)?(?:counting|counts|ticking|ticks|resets|restarts)\b|\bevergreen (?:timer|deadline)\b|"
     r"\b(?:deadline|timer|clock|offer)\s+(?:that\s+)?(?:resets|restarts)\b")
_add("fake_urgency", "ro",
     r"\b(?:fake|manufactur\w+|artificial|false|invented|made[- ]up|phony|bogus|pretend|fabricated|imaginary)\s+" + _g(2) +
     r"(?:urgency|scarcity|deadlines?|fomo|limited|stock|shortage|sell-?out)")
_add("fake_urgency", "ro",
     r"\b(?:creat|add|build|generat|drum up|induc|trigger|manufactur|crank up|dial up|inject)(?:e|es|ed|ing)?\s+" +
     _g(3) + r"(?:(?:sense of )?urgency|scarcity|fomo|fear of missing out|pressure (?:on|to buy))\b")
_add("fake_urgency", "r",
     r"\b(?:say|show|display|write|add|put|tell|claim|announce|pretend|state|mention)\w*\s+.{0,60}?"
     r"\b(?:only|just)\s+(?:\d+|a few|few|one|two|three|five|ten|a handful of)\s+" + _g(3) +
     r"(?:left|remaining|available)\b")
_add("fake_urgency", "r",
     r"\b(?:say|show|display|write|add|put|tell|claim|announce|pretend|state|mention)\w*\s+.{0,60}?"
     r"\b(?:(?:offer|sale|deal|discount|price|promo\w*)\s+(?:ends|expires|goes up|increases|closes)|"
     r"(?:closing|ending|selling out|running out|almost (?:gone|sold out)))\b.{0,80}?"
     r"(?:even though|although|but (?:it|we|really|actually|there)|not really|isn't real|is not real|"
     r"no real|there is no|there's no|doesn't actually|won't actually|forever|every (?:day|visitor|time))")
_add("fake_urgency", "ro",
     r"\b(?:deadline|limit|scarcity|urgency|shortage|cap)\s+" + _g(3) +
     r"(?:that|which)?\s*(?:isn't|is not|is n't|doesn't|does not) (?:real|actually|really|exist)")
_add("fake_urgency", "o",
     r"\b(?:only|just)\s+\d+\s+" + _g(2) + r"(?:left|remaining)\b|"
     r"\b(?:offer|sale|deal|discount|price|promo\w*|enrol?lment|doors?)\s+(?:ends?|expires?|closes?)\s+"
     r"(?:tonight|today|soon|at midnight|in (?:the next )?\d+\s*(?:min\w*|hours?|hrs?|h\b|days?))|"
     r"\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\s*(?:left|remaining)|"
     r"\b(?:hurry|act now|last chance|don't miss out|do not miss out|before it's (?:too late|gone)|"
     r"selling (?:out )?fast|almost (?:gone|sold out)|limited[- ]time (?:offer|only|deal)|"
     r"while (?:supplies|stocks?|spots|seats) last|spots? (?:are )?filling (?:up )?fast|"
     r"price (?:goes|going|will go) up (?:soon|tonight|tomorrow|in \d+)|ends in \d+\s*(?:min\w*|hours?|hrs?))\b")
# Arabic
_add("fake_urgency", "ro",
     r"(?:عد|عداد|موقت|تايمر|ساعه)\s+\S*تنازلي|كاونت ?داون|عد عكسي|عداد عكسي")
_add("fake_urgency", "ro",
     r"(?:استعجال|الحاح|ندره|نقص|ضغط|عجله|موعد نهايي|ديدلاين)\s+" + _g(2) + _AR_FAKE)
_add("fake_urgency", "r",
     r"(?:قول|اكتب|حط|نقول|نكتب|اعلن|ضيف|اضف|خلي|وري|بين)\S*\s+.{0,50}?"
     r"(?:باقي|فاضل|متبقي|تبقي|المتبقي)\s+.{0,12}?(?:\d+|كام|عدد قليل|نسختين|نسخه|مكانين|تلات|ثلاث|خمس)\b.{0,40}?"
     r"(?:بس|فقط|نسخ|اماكن|مقاعد|قطع|ساعات|دقايق)")
_add("fake_urgency", "r",
     r"(?:قول|اكتب|حط|نقول|نكتب|اعلن|ضيف|اضف|خلي)\S*\s+.{0,50}?(?:العرض|الخصم|السعر)\s+"
     r"(?:ينتهي|هينتهي|بينتهي|هيخلص|سينتهي|هيزيد|سيرتفع)\b.{0,80}?"
     r"(?:مع ان|رغم|بس هو|لكن|مش حقيقي|مفيش|في الحقيقه|مش هينتهي|مش هيخلص|لن ينتهي|كل يوم)")
_add("fake_urgency", "o",
     r"(?:باقي|فاضل|متبقي)\s+\d+\s+\S+\s+(?:بس|فقط)|اخر فرصه|الحق (?:العرض|نفسك|قبل)|الكميه محدوده|"
     r"لفتره محدوده|العرض (?:ينتهي|هينتهي|بينتهي|سينتهي) (?:الليله|النهارده|اليوم|قريب|خلال)|سارع|اسرع قبل")

# ---------------- impersonation ----------------
_WHO = (r"(?:the founder|the ceo|the owner|me\b|a customer|customers?|a user|a buyer|a client|the customer|"
        r"an? (?:real |happy |satisfied |paying |verified |independent |neutral |unbiased )?"
        r"(?:customer|user|buyer|client|employee|representative|rep|journalist|reporter|influencer|expert|"
        r"doctor|lawyer|official|investor|reviewer|student|fan|third party|human|person)|someone else|"
        r"another (?:company|brand|person|business|user)|a (?:competitor|rival)|" + _BRANDS + r")")
_add("impersonation", "ro", r"\bimpersonat\w*|\bmasquerad\w*|\bspoof\w*\s+" + _g(3) + r"(?:email|sender|domain|address|identity|number|account)")
_add("impersonation", "ro",
     r"\b(?:pretend|pretending|pretends|pose|posing|poses|pass (?:yourself|ourselves|myself|it|this) off)\s+"
     r"(?:to be|as|you are|you're|that you are|that you're|it is|it's|like you are|like you're)\s+" + _g(3) + _WHO)
_add("impersonation", "ro",
     r"\b(?:act|acting|speak|speaking|write|writing|reply|replying|respond|responding|post|posting|comment|commenting|"
     r"message|messaging|email|emailing|sign|signing|dm|answer|answering|tweet|tweeting|call|calling)\s+" + _g(6) +
     r"(?:(?:as if|as though) (?:you|it|we|this|they) (?:were|was|are|is|came|come|comes)\s+(?:written |sent |posted |signed )?(?:from |by )?" + _WHO + r"|"
     r"as (?:an? )?(?:real |happy |satisfied |paying |verified |independent |neutral |unbiased |random |ordinary )+"
     r"(?:customer|user|buyer|client|reviewer|third party|fan|student)s?|"
     r"as (?:a|an|the) (?:customer|buyer|client|journalist|reporter|influencer|doctor|lawyer|official|competitor)\b"
     r"(?!\s+(?:benefit|persona|journey|story|would|might|sees|facing|support|success|service|onboarding|pain|need|problem|segment|profile|avatar|interview|feedback))|"
     r"as " + _BRANDS + r"\b|"
     r"under (?:a customer's|someone else's|another|a fake|a different|a made-up|a false) (?:person's |brand's |company's )?name|"
     r"while hiding (?:that|who))",
     unless=r"act(?:ing)? as (?:a|an|the) (?:customer|buyer|client) (?:persona|avatar|profile) ")
_add("impersonation", "ro",
     r"\b(?:send|post|publish|reply|email|message|dm|comment|sign|tweet|respond|answer|speak|talk|act|write)\w*\s+.{0,80}?"
     r"(?:in (?:my|the founder's|the ceo's) name|under (?:my|the founder's) name|"
     r"from (?:my|the founder's) (?:personal |own |private )?(?:account|email|inbox|profile|linkedin|twitter|number|whatsapp)|"
     r"on (?:my|the founder's) behalf|signed (?:as|by) (?:me|the founder)|as (?:me|the founder)\b|as if (?:you were|you are|you're|it's|it is|it was) (?:me|the founder))"
     r".{0,80}?(?:without (?:asking|approval|telling|me knowing|my (?:review|approval|knowledge|sign-?off|ok(?:ay)?)|showing|checking|waiting)|"
     r"automatically|don't (?:ask|tell|bother|show|wait)|do not (?:ask|tell|bother|show|wait)|no need to (?:ask|check|show)|"
     r"skip (?:the )?(?:approval|review)|so (?:they|people|nobody|no one) (?:think|believe|know|can tell|won't know)|"
     r"they (?:should|must) (?:think|believe)|never (?:reveal|say|mention|admit|disclose))", neg=False)
_add("impersonation", "ro",
     r"\b(?:never|don't|do not)\s+(?:reveal|say|mention|admit|disclose|tell)\s+" + _g(4) + r"(?:you are|you're|it's|it is|this is|they are talking to)\s+"
     r"(?:an? )?(?:ai|bot|agent|assistant|automated|machine)", neg=False)
_add("impersonation", "ro",
     r"\b(?:claim|say|pretend|imply|state|suggest|write|announce|advertise|make it (?:look|seem|sound) like)\w*\s+(?:that\s+)?" + _g(3) +
     r"(?:we|we're|we are|it is|it's|the product is|i am|i'm|this is|our product is|the app is)\s+" + _g(3) +
     r"(?:endorsed|partnered|affiliated|backed|approved|certified|sponsored|made|built|owned|recommended|officially supported)\s+(?:by|with)\b")
_add("impersonation", "ro",
     r"\b(?:official|officially)\s+" + _g(2) + r"(?:partner|endorsed|approved|certified|affiliated|product|app|store)\b.{0,80}?"
     r"(?:even though|although|we (?:are not|aren't|are n't)|without (?:their|permission|asking)|not actually|not really|nobody will check)", neg=False)
_add("impersonation", "ro",
     r"\b(?:use|copy|clone|steal|rip off|mimic|borrow|put|slap|add)\w*\s+" + _g(4) +
     r"(?:logo|branding|brand name|trademark|identity|name and logo|badge|letterhead)s?\s+" + _g(6) +
     r"(?:so (?:that )?(?:it|we|people|visitors|buyers|they)|to (?:look|seem|appear|make)|without (?:permission|asking|their)|(?:look|seem|appear)s? (?:official|legit\w*|like))")
_add("impersonation", "ro",
     r"\b(?:look|looks|appear|appears|seem|seems|sound|sounds)\s+(?:exactly\s+)?(?:like|as if|as though)\s+"
     r"(?:it's|it is|it|we're|we are|they are|they're|this is|it comes|it came|it was|they|we)\s+" + _g(3) +
     r"(?:from|by|an? official|official|part of|sent by|written by|endorsed by|made by)\s+" + _g(2) +
     r"(?:" + _BRANDS + r"|the founder|a customer|a real customer|customers|someone else|another (?:company|brand)|the government|a bank|their bank)")
_add("impersonation", "ro",
     r"\b(?:fake|bogus|burner|sock ?puppet|made[- ]up)\s+" + _g(2) +
     r"(?:accounts?|profiles?|personas?|identit\w+|email address\w*|senders?|domains?|usernames?)\b")
_add("impersonation", "ro",
     r"\b(?:email|message|post|comment|reply|dm|tweet|letter|press release|statement|quote|endorsement)s?\s+" + _g(4) +
     r"(?:that\s+)?(?:appears?|looks?|seems?|pretend\w*|claims?)\s+(?:to\s+|like\s+)(?:it\s+)?(?:be\s+|is\s+|come\s+|comes\s+|came\s+)?"
     r"(?:from|by|sent by|written by)\b")
_add("impersonation", "ro",
     r"\b(?:from|by|as|signed by|sent by|quote from|statement from|endorsement from|on behalf of)\s+" + _g(2) +
     r"(?:(?:the\s+)?(?:ceo|founder|cto|president|head|support team|team|official account) (?:of|at)\s+)" + _BRANDS,
     unless=r"\]\(https?://|source:|according to")
_add("impersonation", "ro",
     r"\b(?:on behalf of|in the name of|as if (?:from|by|sent by)|signed by|sign (?:it|this|them) as|sender name (?:to|as))\s+" + _g(2) + _BRANDS)
_add("impersonation", "o",
     r"\bas an? (?:verified|real|happy|satisfied|paying|long[- ]time|loyal) (?:customer|user|buyer|client),?\s+i\b|"
     r"\bofficial(?:ly)? (?:partner|endorsed|certified|approved) (?:of|by|with) " + _BRANDS)
# Arabic
_AR_WHO = (r"\S*(?:عميل|زبون|مستخدم|مشتري|المؤسس|المؤسسه|صاحب|مدير|شركه|براند|ماركه|دكتور|محامي|صحفي|"
           r"شخص تاني|حد تاني|شخص اخر|" + _BRANDS + r")")
_add("impersonation", "ro", r"(?:انتحل|انتحال|ينتحل|تنتحل|ننتحل)\S*")
_add("impersonation", "ro",
     r"(?:اتظاهر|تظاهر|يتظاهر|نتظاهر|التظاهر|اعمل نفسك|اعمل انك|مثل انك|ادعي|ادع|تدعي|ندعي|اتقمص|تقمص)\S*\s+.{0,30}?" + _AR_WHO)
_add("impersonation", "ro",
     r"(?:اكتب|رد|ابعت|ارسل|انشر|علق|كلم|اتكلم|تكلم|راسل|وقع|نزل|ردود|رساله|ايميل|بوست|تعليق)\S*\s+.{0,60}?"
     r"(?:كانك|كانها|كانه|كانني|كاننا|كانهم|كاني|علي انك|علي انها|علي انه|علي اني|علي اساس انك|علي اساس انه|بصفتك|بصفه)\s+.{0,25}?" + _AR_WHO)
_add("impersonation", "ro",
     r"(?:باسم|علي لسان|نيابه عن|من طرف)\s+" + _g(2) + r"\S*(?:" + _BRANDS + r"|عميل|عملاء|زبون|شخص تاني|حد تاني|شركه تانيه|شركه اخري)")
_add("impersonation", "ro",
     r"(?:باسم|بدل|مكان)\s*(?:ي|ـي)?\s*(?:المؤسس|صاحب المشروع)?\s*.{0,40}?(?:من غير|بدون|من دون)\s+(?:ما\s+)?"
     r"(?:تسال|اعرف|يعرف|موافق|اذن|ترجع|تقول|علم)", neg=False)
_add("impersonation", "ro",
     r"(?:حساب|حسابات|اكونت|بروفايل|شخصيه|هويه|ايميل)\s+" + _g(1) + _AR_FAKE)
_add("impersonation", "ro",
     r"(?:قلد|تقليد|انسخ|اسرق|استخدم|حط)\S*\s+" + _g(2) + r"(?:شعار|لوجو|علامه|اسم)\s+" + _g(2) + r"\S*" + _BRANDS)

# ---------------- credential_storage ----------------
_SECRET = (r"(?:passwords?|passcodes?|passphrases?|card numbers?|credit cards?|debit cards?|card details|card data|"
           r"card info\w*|cvvs?|cvcs?|pin codes?|pins\b|login credentials|credentials|bank (?:account )?(?:logins?|passwords?)|"
           r"security codes?|2fa codes?|otp codes?|otps?\b|card expir\w+|full card|payment card\w*|cc numbers?)")
_STORE = (r"(?:stor|sav|keep|record|cach|persist|retain|remember|stash|dump|harvest|memoriz|memoris|archiv|back up|backup|write down|hold|hoard)"
          r"(?:e|es|ed|ing|s)?")
_add("credential_storage", "ro",
     r"\b" + _STORE + r"\s+" + _g(6) + _SECRET,
     unless=r"keep(?:s|ing)? (?:the |your |their |all )?(?:passwords?|credentials|cards?|card numbers?) (?:safe|secure|private|out of|away from)|"
            r"remember(?:s|ing)? (?:to|that)|(?:video|course|lesson|guide|tutorial|post|article|tips?|faq|page) (?:on|about)|"
            r"password (?:safety|hygiene|security|policy|strength|reset)")
_add("credential_storage", "ro",
     r"\blog(?:s|ged|ging)?\s+(?:the|their|all|every|each|users?'?|customers?'?|buyers?'?)\s+" + _g(2) + _SECRET)
_add("credential_storage", "ro",
     r"\b(?:database|db|spreadsheet|sheet|table|file|csv|notion|airtable|plain ?text|txt|json|google sheets?|excel|"
     r"vault of our own|column|field|list|doc|document)\s+" + _g(2) + r"(?:of|with|for|containing|holding|that (?:has|holds|stores|lists))\s+" +
     _g(4) + r"(?:passwords?|card numbers?|credit cards?|cvvs?|cvcs?|pins\b|login credentials)")
_add("credential_storage", "ro",
     r"\b(?:passwords?|card numbers?|credit cards?|credit card (?:numbers?|details|info\w*)|cvvs?|cvcs?|card details|login credentials)\s+" +
     _g(5) + r"(?:in|into|to|on|inside)\s+" + _g(3) +
     r"(?:database|db|spreadsheet|sheet|table|file|csv|notion|airtable|plain ?text|txt|json|logs?|google sheets?|excel|server|disk|"
     r"local ?storage|cookies?|memory|repo|repository|config|doc|document|notes?|chat|decision log)\b",
     unless=r"password manager")
_add("credential_storage", "ro",
     r"\b(?:ask|have|get|make|tell|let|require|want|need)\w*\s+" + _g(3) +
     r"(?:customers?|users?|them|buyers?|people|clients?|the founder|subscribers?|everyone)\s+(?:to\s+)?" + _g(2) +
     r"(?:send|email|dm|message|type|share|give|enter|paste|hand over|text|whatsapp|submit|provide|tell)\w*\s+" + _g(5) +
     r"(?:passwords?|card numbers?|credit cards?|cvvs?|card details|logins?\b|credentials|bank login\w*)\b.{0,80}?"
     r"(?:email|chat|dm|form|sheet|whatsapp|message|text|me\b|us\b|here|directly|so (?:i|we|you) can|and (?:keep|store|save|log|remember))")
_add("credential_storage", "r",
     r"\b(?:my|our|the founder's|here is the|here's the|here are the)\s+" + _g(3) +
     r"(?:password|passcode|card number|credit card number|cvv|cvc|login)\s*(?:is\b|are\b|:|=)\s*\S+|"
     r"\b(?:i'll|i will|let me|i can|i'm going to)\s+(?:give|send|share|paste|tell)\s+you\s+" + _g(3) +
     r"(?:password|passcode|card number|credit card|cvv|login|credentials)")
_add("credential_storage", "ro", r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b.{0,40}?(?:cvv|cvc|exp)|(?:cvv|cvc)\s*[:=]?\s*\d{3,4}\b", neg=False)
# Arabic
_AR_SECRET = (r"(?:كلمه السر|كلمات السر|كلمه المرور|كلمات المرور|باسورد|باسوردات|ارقام الكروت|رقم الكارت|"
              r"رقم البطاقه|ارقام البطاقات|ارقام الفيزا|رقم الفيزا|بيانات البطاقه|بيانات البطاقات|بيانات الكارت|بيانات الكروت|"
              r"بيانات الفيزا|الرقم السري|الارقام السريه|ارقام بطاقات|ارقام كروت|cvv|بطاقات الايتمان|بطاقه الايتمان|ارقام الكريدت)")
_add("credential_storage", "ro",
     r"(?:خزن|تخزين|احفظ|حفظ|نحفظ|احتفظ|نحتفظ|نخزن|يخزن|يحفظ|شيل|حط|اكتب|دون|نكتب|نحط|سيف|احتفاظ)\S*\s+.{0,50}?\S*" + _AR_SECRET,
     unless=r"مدير كلمات|تطبيق كلمات")
_add("credential_storage", "ro",
     r"\S*" + _AR_SECRET + r"\S*\s+.{0,40}?(?:في|علي|جوه|داخل)\s+" + _g(2) +
     r"\S*(?:قاعده|داتابيز|شيت|ملف|جدول|اكسل|نوشن|السيرفر|سيرفر|اللوج|لوج|الداتا)")
_add("credential_storage", "ro",
     r"(?:اطلب|خلي|قول|اسال)\S*\s+.{0,40}?(?:يبعت|يبعتوا|يرسل|يرسلوا|يكتب|يكتبوا|يدوا|يدي|يدخل|يدخلوا|يشارك)\S*\s+.{0,30}?\S*" + _AR_SECRET)

# ---------------- money_handling ----------------
_FUNDS = (r"(?:money|funds|cash|revenue|proceeds|payouts?|balance|earnings|takings|profits?|" + _MONEY + r")")
_add("money_handling", "ro",
     r"\b(?:mov|transfer|transferr|wir|send|withdraw|sweep|rout|funnel|divert|forward|pay out|disburs|deposit|remit|cash out|siphon|shift|zelle|venmo)"
     r"(?:e|es|ed|ing|s)?\s+" + _g(5) + _FUNDS + r"\b(?!\s+(?:report|summary|numbers|figures|forecast|projection|estimate|dashboard|model|plan|data|stats|update|chart|table|sheet|breakdown|target|goal))"
     r".{0,80}?\b(?:to|into|from|between|out of)\s+" + _g(5) +
     r"(?:accounts?|bank|wallet|paypal|card|iban|stripe|wise|payoneer|venmo|instapay|vodafone cash|savings|me\b|myself)",
     unless_before=r"(?:customers?|buyers?|users?|clients?|they|people|subscribers?)\s+(?:can\s+|will\s+|to\s+|would\s+|should\s+|could\s+|may\s+)?$")
_add("money_handling", "ro",
     r"\b(?:issu|process|send|giv|approv|trigger|mak|execut|push|do|grant|auto-?approv|fir|releas)(?:e|es|ed|ing|s)?\s+" + _g(4) +
     r"refunds?\b.{0,80}?(?:automatically|without (?:asking|approval|telling|checking|confirm\w*|the founder|me\b|my|waiting|review|a human|human|anyone)|"
     r"by (?:yourself|itself)|on (?:your|its) own|yourself|right now|immediately|directly|instantly|silently|"
     r"don't (?:ask|wait|bother)|do not (?:ask|wait|bother)|no need to (?:ask|wait|check)|skip (?:the )?(?:approval|founder))",
     unless=r"refunds? (?:policy|policies|faq|page|terms)")
_add("money_handling", "ro",
     r"\b(?:automatically|auto|just|go ahead and|silently|quietly|immediately|instantly)[- ]\s*refund(?:s|ed|ing)?\b(?!\s+(?:policy|policies|faq|page|terms))")
_add("money_handling", "ro",
     r"\brefund(?:s|ing)?\s+(?:him|her|them|the (?:customer|buyer|order|payment|charge|client|user)s?|everyone|all|orders?|" + _MONEY + r")\b.{0,80}?"
     r"(?:\bnow\b|without|automatically|yourself|don't (?:ask|wait|bother)|do not (?:ask|wait|bother)|no need to ask|skip (?:the )?approval|"
     r"on your own|by yourself|immediately|right away|asap)")
_add("money_handling", "ro",
     r"\b(?:hold|keep|park|escrow|custody|pool|sit on|safeguard|collect and hold)(?:s|ing|ed)?\s+" + _g(4) +
     r"(?:customers?'?s?|buyers?'?|users?'?|clients?'?|their|the|people's|everyone's)\s+" + _g(2) +
     r"(?:money|funds|payments?|cash|deposits?|balances?)\b",
     unless=r"keep(?:s|ing)? (?:the )?(?:payments?|money) (?:simple|safe|flow|page)")
_add("money_handling", "ro",
     r"\b(?:act as|be|become|run|operate|serve as)\s+" + _g(3) + r"(?:escrow|custodian|money transmitter|middleman for (?:the )?(?:money|payments?|funds))\b")
_add("money_handling", "ro",
     r"\b(?:charg|bill|debit)(?:e|es|ed|ing)?\s+" + _g(5) + r"(?:cards?|customers?|accounts?|them|him|her|users?|buyers?|clients?)\b.{0,60}?"
     r"(?:without (?:asking|approval|telling|consent|permission|them knowing|the founder|their|my|me\b|notice|warning)|automatically again|yourself|silently|secretly|quietly)")
_add("money_handling", "ro",
     r"\b(?:spend|spending|pay|paying|buy|buying|purchase|purchasing|top up|wire|transfer|charge|refund)\s+.{0,70}?"
     r"(?:without (?:asking|approval|my approval|telling|checking|confirm\w*|waiting|my ok(?:ay)?|my sign-?off|the founder|founder approval)|"
     r"don't (?:ask|wait|bother) (?:me|for|the founder)|do not (?:ask|wait|bother) (?:me|for|the founder)|no need to ask|"
     r"skip (?:the )?(?:approval|founder|gate)|i pre-?approve|pre-?approved|approval is not needed|approved in advance)", neg=False,
     unless=r"\b(?:spend|pay)\w* (?:time|attention)")
_add("money_handling", "ro",
     r"\b(?:pay|payments?|money|funds|payouts?|revenue|sales|proceeds|deposits?)\s+" + _g(6) + r"(?:to|into|through|via|in)\s+"
     r"(?:your|the (?:agent|system|bot|ai|platform|tool)(?:'s)?|an? (?:intermediary|pooled|shared|holding|escrow))\s+" + _g(2) +
     r"(?:account|wallet|bank|paypal|balance)")
_add("money_handling", "ro",
     r"\b(?:log ?in ?to|login to|sign in ?to|access|use|open|connect to|get into)\s+(?:my|the founder's|our|the company)\s+" + _g(2) +
     r"(?:bank|paypal|stripe|wise|payoneer|credit card|debit card|card|wallet|venmo|instapay)\b.{0,80}?"
     r"\b(?:send|transfer|withdraw|pay|move|buy|purchase|refund|spend|wire|cash out|top up)")
# Arabic
_AR_FUNDS = r"\S*(?:فلوس|اموال|مبلغ|ارباح|ايرادات|رصيد|دولار|جنيه|مدفوعات|كاش)\S*"
_add("money_handling", "ro",
     r"(?:حول|تحويل|يحول|نحول|انقل|نقل|اسحب|سحب|ابعت|ارسل|ارسال|ودي|حرك|تحريك)\S*\s+.{0,40}?" + _AR_FUNDS +
     r".{0,60}?(?:حساب|محفظه|بنك|فودافون كاش|انستاباي|باي بال|كارت|بطاقه)",
     unless_before=r"(?:العملاء|العميل|الزباين|المشتري|المشترين)\s+(?:\S+\s+)?$")
_add("money_handling", "ro",
     r"(?:رجع|ارجع|استرد|استرجع|رد|نرجع|ترجع|رجعله|رجعلهم)\S*\s+.{0,30}?" + _AR_FUNDS +
     r".{0,60}?(?:من غير|بدون|من دون|تلقاي|اوتوماتيك|لوحدك|بنفسك|فورا|حالا|دلوقتي|علطول|علي طول)")
_add("money_handling", "ro", r"(?:استرداد|استرجاع)\s+" + _g(1) + r"(?:تلقاي\S*|اوتوماتيك\S*|فوري\S*)\s*.{0,30}?(?:من غير|بدون|من دون)")
_add("money_handling", "ro",
     r"(?:احتفظ|خلي|امسك|شيل|نحتفظ|نمسك|احجز)\S*\s+.{0,25}?(?:ب)?(?:فلوس|اموال)\s+(?:ال)?(?:عملاء|زباين|ناس|مشترين)")
_add("money_handling", "ro",
     r"(?:اصرف|ادفع|اشتري|حول|اسحب|انفق|صرف)\S*\s+.{0,60}?(?:من غير|بدون|من دون)\s+(?:ما\s+)?"
     r"(?:تسال|تسالني|موافق|اذن|ترجع|رجوع|تستاذن|استيذان|تقول)", neg=False)

# ---------------- paywall_core ----------------
_CORE = (r"(?:core|essential|basic|main|key|critical|primary|fundamental|must-have|central|base)\s+" + _g(2) +
         r"(?:features?|functionality|functions?|pipeline|steps?|phases?|capabilit\w+|workflow|tools?|parts?|engine|flow|stuff)")
_PAID = (r"(?:behind|until|unless|paywall\w*|pay[- ]wall\w*|pro\b|premium|paid|paying|subscription|subscribers?|pay\b|pays\b|"
         r"upgrade\w*|tier|plan\b|\$\s?\d|credit card|checkout|purchase|buy\b|license key)")
_add("paywall_core", "ro",
     r"\b(?:paywall|pay[- ]wall|gate|lock|hide|restrict|put|move|charge for|charge (?:users|people|founders) for|unlock|reserve|limit|withhold|"
     r"cripple|disable|monetiz|monetis|sell access to)\w*\s+.{0,60}?" + _CORE + r"\b.{0,60}?" + _PAID,
     unless=r"(?:core|essential|basic|main|key)\s+\S+\s+(?:\S+\s+)?(?:free|open)\b|keep\w* .{0,30}(?:core|essential|basic|main)")
_add("paywall_core", "ro",
     _CORE + r"\s+.{0,50}?(?:behind (?:a |our |the |an )?(?:paywall|pay[- ]wall|subscription|paid|premium|pro|upgrade|login and payment)|"
     r"only (?:for|to|in|on|with) (?:the )?(?:paying|paid|premium|pro|subscribers?)|(?:paid|premium|pro)[- ]only|"
     r"require\w* (?:a |an )?(?:payment|subscription|upgrade|paid|credit card))")
_add("paywall_core", "ro",
     r"\b(?:paywall|pay[- ]wall)\w*\s+.{0,40}?(?:the (?:gates?|approval\w*|decision log|guardrails?|pipeline|orchestrator|validation|discover\w*|"
     r"phases?|export|agents?|whole|entire|results?|outputs?)|everything|all (?:the |of the )?(?:features|steps|phases|agents|outputs))")
_add("paywall_core", "ro",
     r"\b(?:make|force|require|get|push)\w*\s+(?:users?|founders?|people|them|everyone|visitors)\s+(?:to\s+)?(?:pay|subscribe|upgrade|buy|enter a card)\b.{0,50}?"
     r"\b(?:to|before|in order to)\b.{0,40}?(?:use|access|run|see|unlock|export|get|view|try|continue)\b.{0,40}?"
     r"(?:core|basic|essential|gates?|approval|pipeline|results?|outputs?|anything|the app|the tool|\bany\b|phase|step|decision log|their own)")
_add("paywall_core", "ro",
     r"\b(?:free (?:tier|version|plan))\s+.{0,50}?(?:useless|crippled|unusable|doesn't (?:really )?work|can't do anything|barely works|worthless|pointless)|"
     r"\bfreemium trap\b|\bbait[- ]and[- ]switch\b")
_add("paywall_core", "ro",
     r"\b(?:hide|lock|gate|blur|withhold|hold back|restrict|put|move|place|keep)\w*\s+" + _g(4) +
     r"(?:the\s+)?(?:results?|outputs?|gate decisions?|decision log|stop memo|validation (?:report|results?)|report|export|phases? \d|steps? \d|later phases)\b.{0,50}?"
     r"(?:until (?:they|users?|the founder|people|he|she) (?:pay|subscribe|upgrade|buy)|behind (?:a |our |the )?(?:paywall|pay[- ]wall|subscription|paid plan|pro plan)|"
     r"unless (?:they|users?|the founder|people) (?:pay|subscribe|upgrade))")
# Arabic
_AR_CORE = (r"\S*(?:مميزات|ميزات|خصايص|وظايف|خطوات|مزايا|مراحل|ادوات|خواص|فيتشرز)\S*\s+\S*(?:اساسيه|رييسيه|مهمه|اساسي|جوهريه)")
_add("paywall_core", "ro",
     r"(?:اقفل|قفل|نقفل|اخفي|خبي|نخبي|احجب|حجب|خلي|نخلي|اجعل|حط|نحط|امنع|نمنع|حدد|اخف)\S*\s+.{0,40}?" + _AR_CORE +
     r".{0,60}?(?:اشتراك|مدفوع|بفلوس|دفع|يدفع|يدفعوا|باي وول|بريميوم|باقه|مشتركين|فلوس|مقابل)")
_add("paywall_core", "ro",
     _AR_CORE + r"\s*.{0,50}?(?:بفلوس|مدفوعه|باشتراك|للمشتركين|ورا جدار|خلف جدار|للمدفوع|مقابل اشتراك|لازم يدفع|بعد الدفع|باي وول|للباقه المدفوعه|مش مجانيه)")
_add("paywall_core", "ro", r"(?:جدار دفع|باي وول|حايط دفع)\s+.{0,40}?(?:اساسي|رييسي|كل حاجه|كل شيء|كل المميزات|كل الخطوات)")

# ---------------- excluded_product ----------------
_PHYS_GOODS = (r"(?:clothes|clothing|apparel|shoes|sneakers|jewel\w+|candles?|soaps?|cosmetics|skincare|perfumes?|furniture|gadgets?|"
               r"phone cases?|toys?|coffee|tea|snacks?|food|meals?|electronics|printed books?|paperbacks?|posters?|stickers?|"
               r"handmade \w+|crafts?|bags?|watches|mugs?|t-?shirts?|tees|hoodies?|hats?|caps|socks|sunglasses|"
               r"supplements?|vitamins?|plants?|flowers?|cakes?|baked goods|pet (?:food|toys|products)|water bottles?|keychains?|"
               r"merch\w*|kitchenware|bikes?|scooters?|drones?)")
_DIGITAL_OK = r"digital|printable|download\w*|pdf|template|virtual|online course|e-?book|svg|png|mockup|icon|font|preset|notion|plugin|theme|software"
_add("excluded_product", "r",
     r"\bcrypto\w*|\bbitcoin|\bbtc\b|\bethereum\b|\bsolana\b|\bdogecoin\b|\bnfts?\b|\bdefi\b|\baltcoins?\b|\bmeme ?coins?\b|\bstablecoins?\b|"
     r"\btoken (?:sale|launch|presale|pre-sale|offering)|\binitial coin offering|\bico (?:launch|sale)|\byield farming|\bstaking (?:pool|reward|platform)|\bcoin (?:launch|offering)",
     sub="financial")
_add("excluded_product", "r",
     r"\btrading (?:signals?|bots?|tips|alerts?|strateg\w+|algorithm\w*|advice|group|room|picks)|\bforex\b|\bfx signals|"
     r"\bstock[- ](?:picks?|tips|trading|signals?|alerts?|picking|advice|recommendations?)|\bday[- ]?trading|\boptions trading|\bcopy[- ]?trading|"
     r"\bswing trading|\bbinary options|\bpenny stocks|\bwhich (?:stocks?|coins?|shares|funds|etfs?|bonds|tokens|assets|securities|options) to (?:buy|sell|hold|invest in)|\b(?:buy|sell)[ /-](?:and[ /-])?(?:sell|hold) signals?|"
     r"\bsignals? (?:group|channel|service|bot|app) for (?:traders|investors|trading)|\bpump and dump|\bponzi|\bpyramid scheme|\bmlm\b|"
     r"\bmulti[- ]level marketing|\bbetting tips|\bsports betting|\bgambling|\bcasino\b|\blottery\b", sub="financial")
_add("excluded_product", "r",
     r"\binvest(?:ment|ing)?\s+(?:advice|advis\w+|tips|products?|funds?|schemes?|plans?|portfolios?|signals?|newsletter|recommendations?|"
     r"picks|app|platform|club|opportunit\w+|returns)|\brobo[- ]?advis\w+|\bportfolio (?:management|advis\w+|rebalanc\w+|allocation advice)|"
     r"\bfinancial (?:advice|advis\w+|products?|planning service|planner service|coaching)|\bwealth management|\bhedge fund|\bmutual funds?\b|"
     r"\b(?:offer|give|provide|sell|issue|grant)\w*\s+" + _g(3) + r"(?:loans?|credit lines?|mortgages?|insurance)\b|"
     r"\bloan (?:app|platform|product|service|marketplace)|\bmicro-?loans?|\bpeer[- ]to[- ]peer lending|\bp2p lending|\bpayday loans?|\bbuy now,? pay later|"
     r"\binsurance (?:products?|polic\w+|plans?|broker\w*|app)|\bcredit (?:repair|score boost\w*)|\btax (?:advice|advis\w+|filing service)|"
     r"\bremittance|\bneobank|\bdigital bank\b|\bsavings account|\binterest[- ]bearing|\bmoney transfer (?:app|service)|\be-?wallet (?:app|product)|"
     r"\b(?:tell|advise|recommend)\w*\s+" + _g(3) + r"(?:what|which|when|where) to (?:invest|buy|sell)", sub="financial")
_add("excluded_product", "r",
     r"\bmedical (?:advice|diagnos\w+|consult\w+|devices?|treatment\w*|recommendations?|guidance|triage)|"
     r"\bhealth (?:advice|coaching|diagnos\w+|supplements?|recommendations?|guidance|treatment\w*)|"
     r"\bdiagnos(?:e|es|ing|is|tic)\s+" + _g(4) + r"(?:symptoms?|diseases?|illness\w*|conditions?|patients?|skin|cancer|diabetes|depression|adhd|anxiety|infections?|rash\w*)|"
     r"\bsymptoms?\s+(?:checker|diagnos\w+|to diagnos\w+|triage)|\b(?:from|based on|using|enter|describe|upload)\w*\s+" + _g(2) + r"symptoms\b|"
     r"\bprescri(?:be|bes|bing|ption|ptions)\b|\bdosages?\b|\bdosing\b|"
     r"\b(?:diet|meal|nutrition|weight[- ]loss|keto|fasting|detox|fat[- ]loss)[\s-]+(?:plans?|advice|program\w*|coaching|coach|protocols?)\b|\blose weight\b|"
     r"\b(?:cure|cures|curing|treat|treats|treating|heal|heals|healing|reverse|reversing)\s+" + _g(3) +
     r"(?:disease\w*|cancer|diabetes|anxiety|depression|illness\w*|acne|insomnia|adhd|arthritis|migraines?|infections?|eczema|pcos)|"
     r"\b(?:therapy|therapist|counsell?ing|psychotherap\w+|psychiatr\w+)\s+(?:bot|app|chatbot|service|sessions?|ai|platform)|"
     r"\bai[- ](?:powered )?(?:therapist|doctor|nurse|physician|dietitian|nutritionist|psychologist|psychiatrist|pharmacist)|"
     r"\b(?:virtual|robot|chatbot|online) (?:therapist|doctor|physician|dietitian|nutritionist)\b|\btelemedicine|\btelehealth|"
     r"\bmental[- ]health (?:advice|app|chatbot|coach\w*|support bot|treatment|therapy|diagnos\w+)|"
     r"\b(?:medications?|medicines?|drugs?|pills?|antibiotics?|insulin)\s+(?:advice|recommend\w+|to take|reminder with dos\w+|interaction\w*)|"
     r"\bwhat (?:medicine|medication|drug|pills?) to take|\b(?:recommend|suggest)\w*\s+" + _g(2) + r"(?:medications?|medicines?|treatments?|drugs)\b",
     sub="health")
_add("excluded_product", "r",
     r"\b(?<!not )(?<!no )(?<!isn't )(?<!without )legal (?:advice|advis\w+|counsel|opinions?|representation|consult\w+|help bot|services?|guidance|assistant|chatbot|bot|aid app)|"
     r"\bai[- ](?:powered )?(?:lawyer|attorney|paralegal|legal \w+|solicitor|judge)|\b(?:lawyer|attorney|solicitor)[- ]?(?:bot|ai|chatbot|in your pocket|app)|"
     r"\b(?:robot|virtual|chatbot|online) (?:lawyer|attorney|solicitor)\b|"
     r"\b(?:draft|write|generate|review|prepare)\w*\s+" + _g(3) +
     r"(?:legally binding|contracts? for (?:clients|customers|users|people)|wills\b|last will|living wills?|lawsuits?|legal (?:contracts?|documents?|agreements?|filings?|letters?)|"
     r"divorce \w+|immigration (?:forms?|applications?|petitions?)|visa applications?|court (?:filings?|documents?)|cease[- ]and[- ]desist)|"
     r"\b(?:immigration|visa|divorce|lawsuit|litigation|contract law|tenant rights?|tenancy|employment law|custody|inheritance|eviction|criminal defen[cs]e)\s+"
     r"(?:advice|advis\w+|consult\w+|guidance|help bot|assistant|chatbot|bot)|\b(?:replace|replaces|replacing|instead of|without)\s+(?:a |an |your |the )?(?:lawyer|attorney|solicitor)|"
     r"\b(?:tell|advise|advising)\w*\s+" + _g(3) + r"(?:whether|if|how) (?:to|they can|they should|you can|you should) sue\b",
     sub="legal", neg=False)
_add("excluded_product", "r",
     r"\bphysical (?:goods?|products?|items?|merch\w*|inventory|copies|books?|store|shop|kits?|planners?)|\bdrop[- ]?ship\w*|\bprint[- ]on[- ]demand|"
     r"\bsubscription box\w*|\b3d[- ]print(?:ed|ing) (?:products?|items?|parts?|models? to sell|toys?|figur\w+)|\bhardware (?:products?|devices?|gadgets?|startup|kits?)|"
     r"\biot devices?|\bwearable (?:device|product|gadget)s?|"
     r"\b(?:open|opening|start|starting|run|running|launch|launching)\s+" + _g(3) + r"(?:restaurant|food truck|bakery|coffee shop|cafe|cloud kitchen|boutique|grocery)\b|"
     r"\b(?:ship|shipping|shipped|deliver|delivering)\s+" + _g(3) + r"(?:physical|boxes|parcels|packages|by courier|to (?:their|your|the customer's) door)|"
     r"\be-?commerce (?:store|shop|brand) (?:for|selling|that sells)\b", sub="physical")
_add("excluded_product", "r",
     r"\b(?:sell|selling|sells|ship|shipping|deliver|delivering|manufactur\w+|resell\w*|retail\w*|produce|producing|bake|baking|"
     r"sew|sewing|print|printing)\s+" + _g(4) + _PHYS_GOODS + r"\b",
     sub="physical", unless=_DIGITAL_OK + r"|(?:course|guide|tutorial|lessons?|newsletter|blog|community|app|tool|tracker|directory) (?:on|about|for)")
_add("excluded_product", "r",
     r"\b(?:store|shop|brand|business|startup|line)\s+(?:selling|that sells|for|of)\s+" + _g(3) + _PHYS_GOODS + r"\b|"
     r"\b" + _PHYS_GOODS + r"\s+(?:brand|store|shop|line|business|company)\b|\b(?:my|our|own|a|new)\s+(?:clothing|apparel|fashion|jewel\w+|candle|skincare|"
     r"cosmetics|perfume|furniture|toy|coffee|tea|snack|supplement|merch\w*) (?:brand|line|store|shop|business|label)\b",
     sub="physical", unless=_DIGITAL_OK + r"|(?:for|helps?|to help|aimed at|serving) (?:\S+\s+){0,3}(?:brands?|stores?|shops?|businesses|owners?|companies)")
# Arabic
_add("excluded_product", "r",
     r"عملات رقميه|عمله رقميه|عملات مشفره|العملات المشفره|كريبتو|بيتكوين|بتكوين|ايثيريوم|اثيريوم|"
     r"توصيات\s+" + _g(1) + r"\S*(?:تداول|اسهم|فوركس|عملات|بورصه|استثمار)\S*|(?<!\S)(?:و)?(?:ال|لل|بال)?تداول|فوركس|"
     r"\S*بورصه|(?:شراء|بيع|تشتري|تبيع|اشتري|توصيات)\s+" + _g(1) + r"(?:ال)?اسهم|"
     r"(?:نصايح|نصيحه|استشار\S+|توصيات|مشوره)\s+" + _g(1) + r"\S*(?:استثمار\S*|ماليه|مالي)|"
     r"(?:منتج|تطبيق|منصه|خدمه|صندوق|محفظه)\s+" + _g(1) + r"\S*(?:استثمار\S*|تمويل\S*|قروض|تامين)|"
     r"(?:تقديم|نقدم|اقدم|منح)\s+" + _g(1) + r"\S*(?:قروض|قرض|سلف)|استثمر فلوسك|تستثمر فلوس|مراهنات|(?<!\S)(?:ال)?قمار", sub="financial")
_add("excluded_product", "r",
     r"تشخيص\s+" + _g(2) + r"\S*(?:امراض|مرض|طبي|اعراض|حالات|حاله|جلد)\S*|يشخص\S*|بيشخص\S*|تشخص\S*|"
     r"(?:وصف|يوصف|بيوصف|توصف|يكتب|بيكتب)\S*\s+" + _g(1) + r"\S*(?:ادويه|علاج|دواء|دوا|روشته)\S*|جرعات|جرعه|"
     r"(?:علاج|يعالج|بيعالج|تعالج)\s+" + _g(1) + r"\S*(?:سكر|سرطان|اكتياب|قلق|امراض|مرض|ضغط|ارق|حب الشباب)\S*|"
     r"نظام غذايي|انظمه غذاييه|رجيم|دايت|تخسيس|انقاص الوزن|مكملات غذاييه|"
     r"(?:دكتور|طبيب|معالج|اخصايي تغذيه)\s+" + _g(1) + r"\S*(?:ذكاء|الي|افتراضي|ai|روبوت|نفسي)\S*|"
     r"(?:استشار\S+|نصايح|نصيحه|مشوره)\s+" + _g(1) + r"\S*(?:طبيه|طبي|صحيه|علاجيه|نفسيه)|علاج نفسي", sub="health")
_add("excluded_product", "r",
     r"(?:استشار\S+|نصايح|نصيحه|مشوره|فتاوي|فتوي|خدمات)\s+" + _g(1) + r"\S*(?:قانونيه|قانوني)|"
     r"محامي\s+" + _g(1) + r"\S*(?:الي|ذكاء|ai|روبوت|افتراضي|اونلاين)\S*|"
     r"(?:صياغه|كتابه|يكتب|بيكتب|يصيغ|بيصيغ|اكتب)\s+" + _g(1) + r"\S*(?:عقود|وصايا|دعاوي|مذكرات قانونيه)\S*\s*" + _g(1) + r"\S*(?:قانونيه|ملزمه|للعملاء|للناس|رسميه)|"
     r"بدل (?:ال)?محامي|بديل (?:ال|لل)?محامي|من غير محامي", sub="legal", neg=False)
_add("excluded_product", "r",
     r"منتجات (?:ماديه|فعليه|ملموسه)|منتج (?:مادي|ملموس)|دروب ?شيب\S*|طباعه عند الطلب|"
     r"(?:ابيع|بيع|نبيع|تبيع|هبيع|يبيع|بيبيع|متجر|محل|براند|ماركه|اشحن|شحن|نشحن|تصنيع|اصنع|نصنع|استيراد|استورد)\s+" + _g(3) +
     r"\S*(?:ملابس|هدوم|احذيه|جزم|عطور|برفانات|اكسسوارات|شنط|موبايلات|اكل|حلويات|مجوهرات|شموع|صابون|مستحضرات|ساعات|قهوه|"
     r"كتب مطبوعه|منتجات يدويه|هاند ميد|اجهزه|تيشيرتات|تيشرتات|بضاعه|العاب اطفال|اثاث|مكملات)\S*|"
     r"(?:افتح|فتح|نفتح|اعمل|ابدا)\s+" + _g(1) + r"(?:مطعم|مخبز|كافيه|محل|سوبر ماركت)",
     sub="physical", unless=r"رقمي|ديجيتال|قوالب|كورس|دوره|تطبيق ل|برنامج ل")
# M5: disguised excluded products (a medical condition plus food/dose guidance; telling people what to buy)
_CONDITION = (r"(?:diabet\w*|blood sugar|glucose|insulin|hypertension|high blood pressure|celiac|coeliac|pcos|ibs\b|"
              r"kidney disease|heart disease|cancer patients?|crohn\w*)")
_GUIDANCE = (r"(?:meals?|diet\w*|carbs?|carbohydrates?|food|eat\w*|dos(?:e|es|ing|age)|portions?|recipes? plan\w*|"
             r"meal plan\w*|adjust\w*|recommend\w*|advice|advis\w*|coach\w*|tells? (?:them|people|users|you) (?:what|how much))")
_add("excluded_product", "r",
     r"\b" + _CONDITION + r"\b.{0,80}?\b" + _GUIDANCE + r"|\b" + _GUIDANCE + r"\b.{0,80}?\b" + _CONDITION + r"\b",
     sub="health")
_AR_CONDITION = r"(?:مرضي|مريض|لمرضي|للمرضي|مرض|لمريض)\s+(?:ال)?(?:سكر|سكري|ضغط|قلب|كلي|سرطان|كبد)"
_AR_GUIDANCE = r"\S*(?:وجبات|وجبه|اكل|اكلات|غذا|اطعمه|جرعات|جرعه|انسولين|رجيم|نظام|يقترح|تقترح|ينصح|نصايح)\S*"
_add("excluded_product", "r", _AR_GUIDANCE + r".{0,60}?" + _AR_CONDITION + r"|" + _AR_CONDITION + r".{0,60}?" + _AR_GUIDANCE,
     sub="health")
_add("excluded_product", "r",
     r"(?:يشتر|تشتر|نشتر|اشتر|يبيع|تبيع|نبيع|بيع|شراء|يستثمر|تستثمر)\S*\s+" + _g(2) +
     r"(?:ال)?(?:اسهم|سهم|عملات|عمله|صناديق|سندات)(?:\s|$|[,.?!])", sub="financial")
# Output-only advice shapes
_add("excluded_product", "o",
     r"\b(?:you should|we recommend(?: that you)?|i recommend(?: that you)?|our advice is to)\s+" + _g(3) +
     r"(?:buy|sell|invest in|short|hold)\s+" + _g(3) + r"(?:stocks?|shares|tokens?|coins?|funds?|\$[a-z]{2,5}\b)",
     sub="financial")
_add("excluded_product", "o",
     r"\btake\s+\d+\s?(?:mg|ml|mcg|tablets?|pills?|capsules?)\b", sub="health")


# ---------------- second-round rules: plain rewordings ----------------
# Proof that is to be made up, said in two clauses ("... quotes ...; just make them up").
_PROOF_WORD = r"(?:quotes?|testimonials?|reviews?|endorsements?|success stor\w+|case stud\w+|ratings?|social proof|sign-?ups?)"
_NO_BUYERS = (r"(?:(?:we|i)\s+(?:have|'ve|got|do not have|don't have|haven't)\s+(?:got\s+)?(?:no|zero|not|any|n't)\b|"
              r"(?:no|zero)\s+(?:sales|buyers|customers|users|clients|sign-?ups)\b|"
              r"nobody (?:has\s+)?(?:bought|signed|used|paid)|no one (?:has\s+)?(?:bought|signed|used|paid)|"
              r"before anyone (?:buys|has bought|signs|has signed|pays)|haven't launched|not launched|pre-?launch)")
_MAKE_UP = (r"(?:\b(?:mak(?:e|ing)|made)\s+(?:them|it|some|those|these|a few|one|something)\s+up\b|"
            r"\b(?:invent|fabricat(?:e|ing)|fake|imagine)\s+(?:them|some|those|these|a few|one)\b|\bout of thin air\b)")
_add("fake_social_proof", "ro", r"\b" + _PROOF_WORD + r"\b.{0,160}?" + _MAKE_UP)
_add("fake_social_proof", "ro", _MAKE_UP + r".{0,80}?\b" + _PROOF_WORD + r"\b")
_add("fake_social_proof", "ro",
     r"\b(?:put|add|show|display|place|include|write|post|publish|feature|need|want)\w*\s+" + _g(4) +
     r"(?:quotes?|testimonials?|reviews?|endorsements?)\s+(?:from|by)\s+" + _g(2) +
     r"(?:buyers|customers|users|clients|founders|freelancers|students|members|people)\b.{0,120}?" + _NO_BUYERS)

# Someone's name or endorsement used without their consent.
_NO_CONSENT = (r"(?:without\s+(?:her|his|their|asking|permission|consent|them knowing|telling|any agreement)|"
               r"(?:she|he|they)\s+(?:has|have|did)\s+not\s+(?:agreed?|consent\w*|approved?|said yes|given)|"
               r"(?:she|he|they)\s+(?:hasn't|haven't|didn't|never)\s+(?:agreed?|consent\w*|approved?|said yes|given)|"
               r"(?:she|he|they)\s+(?:does not|doesn't|do not|don't|will not|won't)\s+know|not agreed|never agreed)")
_add("impersonation", "ro",
     r"\b(?:with|under|using)\s+the\s+names?\s+of\s+(?:a|an|some|another)\s+" + _g(2) +
     r"(?:influencer|celebrit\w+|expert|doctor|professor|customer|competitor|journalist|brand|person|author|athlete|"
     r"youtuber|creator|public figure|investor|blogger|streamer|star)\b")
_add("impersonation", "ro",
     r"\b(?:so|to make|make|making)\s+(?:that\s+)?it\s+(?:looks?|seems?|appears?|sounds?)\s+(?:like|as if|as though)\s+"
     r"(?:he|she|they|\S+)\s+(?:\S+\s+)?(?:endorses?|endorsed|recommends?|recommended|approves?|approved|backs?|backed|"
     r"supports?|vouch\w*|partner\w*|wrote (?:it|this)|said (?:it|this)|signed)\b")
_add("impersonation", "ro",
     r"\b(?:us(?:e|es|ing)|put|putting|add|adding|attach\w*|borrow\w*|drop\w*)\s+" + _g(4) +
     r"(?:names?|endorsements?|face|photos?|pictures?|likeness|signatures?|quotes?|logo)\b.{0,80}?" + _NO_CONSENT, neg=False)
_add("impersonation", "ro",
     r"\b(?:endors\w+|recommend\w+|vouch\w*)\b.{0,80}?" + _NO_CONSENT + r"|" + _NO_CONSENT + r".{0,60}?\b(?:endors\w+|vouch\w*)\b",
     neg=False)

# Getting rich / no risk.
_add("income_promise", "ro",
     r"\b(?:be|become|becomes|becoming|get|gets|getting|make you|makes you|made me|make them|makes them)\s+"
     r"(?:rich|wealthy|a millionaire|millionaires|financially free|filthy rich)\b")
_add("income_promise", "ro",
     r"\b(?:zero|no|without any|without)\s+risk\b.{0,80}?\b(?:income|earn\w*|profits?|money|rich|returns?|wealth\w*)\b|"
     r"\b(?:income|earn\w*|profits?|rich|returns?|wealth\w*)\b.{0,80}?\b(?:zero|no|without any|without)\s+risk\b", neg=False)
_AR_RICH = r"(?:غني|اغنياء|اغنيا|غنيا|ثري|اثرياء|ثريا|مليونير|مليونيرات|اثريا)"
_add("income_promise", "ro",
     r"(?<!\S)(?:و|ف)?(?:س|ه|ح)?(?:ت|ي|ن|ا)?(?:صبح|بقي|بقو|كون|صير)\S*\s+" + _g(1) + r"(?:ال)?" + _AR_RICH)
_add("income_promise", "ro",
     r"(?:هتغتني|ستغتني|تغتني|يغتنوا|يغتني|سيغتنون|الثراء|تحقيق ثروه)|"
     r"(?<!\S)(?:و|ف)?(?:ست|سوف ت|هت|سي|هي)حقق\S*\s+" + _g(2) + r"\S*(?:ارباح|دخل|ثروه|مكاسب|الاف|ملايين)")
_AR_NO_RISK = r"(?:بلا|بدون|من غير|من دون|ولا)\s+(?:اي\s+)?(?:مخاطره|مخاطر|مجازفه|خساره|ريسك)"
_add("income_promise", "ro",
     _AR_NO_RISK + r".{0,80}?(?:" + _AR_INCOME + r"|" + _AR_RICH + r")|(?:" + _AR_INCOME + r"|" + _AR_RICH + r").{0,80}?" + _AR_NO_RISK,
     neg=False, unless=r"استرداد|استرجاع")

# Money sent back / charges reversed with no founder decision.
_REFUND_ACT = (r"(?:\b(?:send|sends|sending|give|gives|giving|pay|pays|paying|return|returns|returning|wire|transfer|hand|credit)\s+" +
               _g(4) + r"(?:payments?|money|funds|cash|purchase price|amount|charges?|fees?|" + _MONEY + r")\s+back\b|"
               r"\b(?:send|give|pay|hand)(?:s|ing)?\s+back\s+" + _g(4) + r"(?:payments?|money|funds|cash|amount|charges?)\b|"
               r"\breturn(?:s|ing)?\s+" + _g(4) + r"(?:payments?|money|funds)\b|"
               r"\breimburs\w+|\brevers(?:e|es|ing)\s+" + _g(3) + r"(?:charges?|payments?|transactions?)\b|\bchargebacks?\b|"
               r"\brefund(?:s|ed|ing)?\b(?!\s+(?:policy|policies|faq|page|terms|request|requests|window|period)))")
_AUTONOMY = (r"(?:\bautomatically\b|\bauto-|\bwithout (?:asking|checking with|telling|consulting|waiting for) (?:me|the founder|anyone|first)\b|"
             r"\bwithout (?:my |the founder's |founder |any |prior )?(?:approval|permission|sign-?off|ok|okay|review|confirmation)\b|"
             r"\bon (?:your|its) own\b|\bby (?:yourself|itself)\b|\b(?:don't|do not|never) (?:ask|check with|wait for|bother) (?:me|the founder)\b|"
             r"\bno need to (?:ask|check|confirm|wait)\b|\bskip (?:the )?(?:approval|founder)\b)")
_NOT_AUTO = (r"(?:never|not|n't)\s+(?:be\s+)?(?:(?:issued|sent|processed|made|done|given|paid|returned|refunded|reversed)\s+)?"
             r"(?:automatically|by itself|on its own)")
_add("money_handling", "ro", _AUTONOMY + r".{0,100}?" + _REFUND_ACT, unless=_NOT_AUTO)
_add("money_handling", "ro", _REFUND_ACT + r".{0,100}?" + _AUTONOMY, unless=_NOT_AUTO)


# ---------------- M5: indirect attacks ----------------
# Repeating one invented item many times ("that review was perfect, now 49 more").
_add("fake_social_proof", "r",
     r"\b" + _PROOF_WORD + r"\b.{0,200}?\b(?:write|generate|create|make|give me|do|produce|repeat|copy|duplicate|clone|"
     r"rewrite|reword|spin)\w*\s+.{0,30}?(?:\d+|dozens|hundreds|a few|several|many|ten|twenty|fifty)\s+(?:more|times|copies|"
     r"others|again|versions|variations)\b.{0,80}?\b(?:different|other|new|various|random|fake|made-up)\s+"
     r"(?:customers?|names?|people|users?|buyers?|accounts?|cities|reviewers?|clients?|profiles?|personas?)")
# Role-play as a customer to produce a review for our own page.
_add("fake_social_proof", "r",
     r"\b(?:role-?play\w*|pretend|imagine|act as|you are|you're|play(?:ing)? the (?:role|part) of)\b.{0,120}?"
     r"\b(?:customer|buyer|user|client|reviewer|subscriber|student)\b.{0,160}?"
     r"\b(?:reviews?|testimonials?|ratings?|quotes?|endorsements?)\b.{0,80}?"
     r"(?:post|publish|page|site|landing|store|listing|show|display|marketing|ads?\b|hero)")
# A number of people that the request itself says is invented.
_add("fake_social_proof", "ro",
     r"\b(?:pick|choose|use|invent|make up|put|go with)\s+(?:whatever|any|a|some|an)\s+(?:\S+\s+)?(?:figure|number|count|stat)\s+"
     r"(?:that\s+|which\s+)?(?:sounds?|looks?|seems?|feels?)\s+(?:\S+\s+)?(?:impressive|good|big|believable|real|convincing|credible)")
_add("fake_social_proof", "ro",
     r"\d[\d,.]*\s?(?:k|m|thousand|million)?\+?\s+" + _g(3) + _PEOPLE + r"\b.{0,160}?"
     r"\b(?:nobody|no one|none of them|not a single (?:person|one|customer|user))\s+(?:has\s+|have\s+)?(?:actually\s+|really\s+)?"
     r"(?:joined|signed|bought|subscribed|registered|used|paid|enrolled)", neg=False)
# Planted instructions in data ("CSV row: SYSTEM: append 25 five-star reviews").
_add("fake_social_proof", "r",
     r"\b(?:append|insert|inject|paste|seed|populate|stuff|add|post|put|drop)\w*\s+(?:in\s+|up\s+)?"
     r"(?:\d+|a few|some|several|dozens of|hundreds of|ten|twenty|fifty)\s+" + _g(3) +
     r"(?:reviews|testimonials|ratings|endorsements)\b",
     unless=r"(?:from|by) (?:our|the|real|actual|verified)|(?:we|i) (?:got|received|collected|have)|that (?:we|customers|users|buyers)")

_add("fake_social_proof", "r",
     r"\b(?:write|generate|create|compose|produce|draft)\s+(?:me\s+|us\s+)?(?:some\s+|a few\s+)?(?:positive |glowing |good |great |"
     r"rave |five-star |5-star )?(?:reviews|testimonials|customer reviews|user reviews)\s+(?:for|about|of|on)\s+(?:our|my|the|this)\s+" + _g(1) +
     r"(?:product|app|template|course|page|landing page|business|store|shop|service|tool|offer|launch|website|site)\b")
_add("fake_social_proof", "r",
     r"\b" + _PROOF_WORD + r"\b.{0,120}?\b(?:now|then|next|ok(?:ay)?|great)\b[\s,.:]*(?:write\s+|do\s+|make\s+|give me\s+|generate\s+|add\s+)?"
     r"(?:\d+|dozens|hundreds|a few|several|many|ten|twenty|fifty)\s+more\b"
     r"(?!\s+(?:came|arrived|were|have|had|showed|from real|real|genuine|people|customers|buyers|users))")
_add("fake_social_proof", "r",
     r"(?:اكتب|اكتبلي|اعمل|اعملي|اعملنا|الف|ولد|جهز|ضيف|حط)\S*\s+(?:لي\s+|لنا\s+|ليا\s+)?(?:\S+\s+)?"
     r"(?:تقييمات|ريفيوهات|ريفيوز|مراجعات|شهادات عملاء|اراء عملاء|كومنتات)",
     unless=r"سياسه|طلب|ازاي|كيف|نموذج|رساله|ايميل|نطلب|اطلب|اسال|حقيقي")

# Phase 2 (landing page) requests: a claimed count, quotes with no real source, a ticking timer.
_REAL_COUNT = r"\b(?:real|actual|actually|so far|exactly|measured|from (?:the|our) (?:csv|export|data|waitlist file))\b"
_add("fake_social_proof", "r",
     r"\b(?:say|claim|state|write|show|display|put|add|announce|mention|tell (?:people|visitors|them))\w*\s+(?:that\s+)?.{0,20}?"
     r"\d[\d,.]*\s?(?:k|m|thousand|million)?\+?\s+" + _g(2) + _PEOPLE + r"\s+" + _g(2) +
     r"(?:joined|signed up|subscribed|registered|enrolled|bought|are on|on the (?:waitlist|list)|already)\b",
     unless=_REAL_COUNT)
_add("fake_social_proof", "r",
     r"\bjoin\s+(?:over\s+|more than\s+|the\s+)?\d[\d,.]*\s?(?:k|m|thousand|million)?\+?\s+" + _g(1) + _PEOPLE +
     r"\b(?!\s+(?:for|at|in a|on a|on an)\b)",
     unless=_REAL_COUNT)
_add("fake_social_proof", "r",
     r"\b(?:put|add|show|display|place|include|insert|drop|sprinkle)\w*\s+" + _g(4) +
     r"(?:customer|user|client|buyer|member|student|reader)s?'?\s+(?:quotes?|testimonials?|reviews?|stories|praise)\b",
     unless=r"(?:from|by) (?:our|the|real|actual|verified)\b|(?:we|i) (?:got|received|collected|have)\b|interview|survey|"
            r"with (?:their )?permission|request|ask\w* for")
_add("fake_urgency", "r",
     r"\btimer\b.{0,60}?\b(?:ticks?|ticking|counts?|counting)\s+down\b|"
     r"\b(?:timer|clock|banner|counter)\b.{0,40}?\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b")
_add("fake_social_proof", "r",
     r"(?:اكتب|قول|حط|نقول|نكتب|اعلن|ضيف|وري)\S*\s+.{0,40}?(?:\d+|الف|الاف|مليون)\s+.{0,10}?"
     r"(?:شخص|واحد|عميل|عملاء|مستخدم|مصمم|مصممين|مشترك|ناس)\S*\s+.{0,10}?(?:سجل|اشترك|انضم|اشتري|دخل)\S*",
     unless=r"حقيقي|فعلا|فعليا|لحد دلوقتي|لحد الان|حتي الان")

# ---------------- approval_bypass ----------------
_SIDE_EFFECT = (r"(?:publish\w*|post\w*|send\w*|sent|email\w*|launch\w*|deploy\w*|go(?:es)? live|tweet\w*|announce\w*|sign\w*|"
                r"submit\w*|spend\w*|pay\w*|charg\w*|refund\w*|buy\w*|purchas\w*|run (?:the )?ads?|blast\w*|message\w*|dm\w*|"
                r"push\w* (?:it|the page|live)|put (?:it|the page) live|ship\w*|release\w*|upload\w*)")
_add("approval_bypass", "r",
     r"\b(?:the founder|founder|the ceo|ceo|the owner|owner|my boss|the boss|admin|the admin|management|the team|i|we)\s+"
     r"(?:has\s+|have\s+|had\s+|'s\s+|already\s+|just\s+|actually\s+)*"
     r"(?:approved|allowed|authori[sz]ed|signed off|okayed|ok'd|green-?lit|said yes|pre-?approved|gave (?:the |you )?(?:ok|okay|go-ahead|green light|permission|approval))"
     r"\b.{0,120}?\b" + _SIDE_EFFECT, neg=False)
_add("approval_bypass", "r",
     r"\bas (?:an? |the |your )?(?:admin\w*|administrator|developer|dev|operator|owner|root|superuser|system|moderator|maintainer|"
     r"ceo|founder|boss|manager)\b\s*,?\s*(?:i\s+)?(?:allow|authori[sz]e|approve|permit|override|grant|order|instruct|tell)\w*", neg=False)
_add("approval_bypass", "r",
     r"\b(?:skip|bypass|disable|turn off|switch off|remove|ignore|circumvent|get around|work around|override|suppress|silence|drop)\w*\s+" + _g(3) +
     r"(?:founder(?:'s)?\s+)?(?:approval|confirmation|sign-?off|consent|permission)(?:\s+(?:step|prompt|check|gate|process|screen|dialog))?\b")
_add("approval_bypass", "r",
     r"\b" + _SIDE_EFFECT + r"\b.{0,100}?(?:without (?:asking|approval|waiting|confirmation|confirming|the founder|founder approval|my approval|"
     r"checking|a prompt|prompting|review|(?:showing|telling|asking|checking with) (?:me|the founder|them)|me (?:seeing|checking|knowing)|"
     r"my (?:ok|okay|review|sign-?off|go-ahead))|no (?:need (?:to|for)|approval|confirmation|sign-?off)\b|(?:don't|do not|never) (?:ask|wait|prompt|check)\b|"
     r"(?:approval|confirmation|sign-?off) (?:is )?(?:not |no longer )?(?:needed|required|necessary)\b)")
_add("approval_bypass", "r",
     r"\bauto[- ]?approv\w*|\bskip_approval\b|--yes\b|\bassume[- ]yes\b|\bapprove (?:everything|all|every\w*|anything|it all)\b|"
     r"\b(?:treat|count|mark|consider|take)\s+(?:every|all|each|any)\s+(?:approval|request|prompt|question)s?\s+as\s+(?:an?\s+)?(?:yes|approved|approval)|"
     r"\balways (?:say|answer|reply|click) yes\b|\bapprove (?:it|them) (?:yourself|on (?:my|the founder's) behalf|for (?:me|the founder))")
_add("approval_bypass", "r",
     r"\b(?:ignore|disregard|forget|override|bypass|drop)\s+" + _g(3) +
     r"(?:rules|instructions|guardrails|restrictions|policies|safety|safeguards|limits|constraints|system prompt)\b")
# Arabic
_AR_SIDE = (r"(?:انشر|نشر|تنشر|ينشر|ننشر|ابعت|تبعت|يبعت|نبعت|ارسل|ترسل|يرسل|نزل|تنزل|اطلق|بوست|ادفع|تدفع|اصرف|اشتري|وقع|توقع|"
            r"رجع|ترجع)\S*")
_add("approval_bypass", "r",
     _AR_SIDE + r".{0,80}?(?:من غير|بدون|من دون)\s+(?:ما\s+)?(?:\S+\s+)?(?:تسال|تساله|تسالها|تسالني|موافق|موافقه|اذن|تستاذن|ترجع|مراجعه|ما حد|تاكيد)", neg=False)
_add("approval_bypass", "r",
     r"(?:المؤسس|صاحب المشروع|المدير|الادمن|الادارة|الاداره)\s+(?:\S+\s+){0,2}(?:وافق|موافق|سمح|اذن|اعتمد)\S*.{0,80}?" + _AR_SIDE, neg=False)
_add("approval_bypass", "r",
     r"بصفتي\s+(?:ال)?(?:ادمن|مدير|مطور|المؤسس|صاحب)\S*|(?:تجاهل|انسي|اتجاهل|سيبك من)\s+.{0,25}?(?:القواعد|التعليمات|القيود|الضوابط|الشروط)|"
     r"(?:وافق|اوافق)\s+علي\s+(?:كل|اي)\s+(?:حاجه|شيء|طلب)|(?:تخطي|اتخطي|اتجاوز|تجاوز|الغي|شيل)\s+.{0,20}?(?:الموافقه|خطوه الموافقه|التاكيد)", neg=False)

_add("approval_bypass", "r",
     r"\b(?:the founder|founder|the ceo|ceo|the owner|owner|my (?:boss|manager|partner|cofounder|co-founder)|the (?:boss|manager)|"
     r"admin|management|legal|the team)\s+(?:is|are|'s)\s+(?:totally\s+|completely\s+)?(?:fine|ok|okay|cool|good|happy)\s+with\s+(?:it|this|that)"
     r"\b.{0,120}?\b" + _SIDE_EFFECT, neg=False)
_add("approval_bypass", "r",
     _AR_SIDE + r".{0,80}?(?:متستناش|ماتستناش|متستنيش|ماتستنيش|بلاش تستني|من غير ما تستني|متسالنيش|ماتسالنيش|متسالش|ماتسالش|"
     r"متاخدش موافق|ماتاخدش موافق)", neg=False)
# A claimed tie to another brand that the request itself says is not true.
_add("impersonation", "r",
     r"\b(?:officially\s+)?(?:partnered|affiliated|endorsed|sponsored|certified|backed|approved|recommended)\s+(?:with|by)\s+" + _g(2) +
     _BRANDS + r"\b.{0,60}?(?:even though|although|but (?:we|it|they) (?:are|is|were|was)n?'?t?|not true|isn't|is not|we are not|"
     r"we aren't|it is not|it's not|they are not|they aren't|never (?:agreed|were|was))", neg=False)

# ---------------- gate_tampering ----------------
_TARGET = (r"(?:targets?|thresholds?|bar|cut-?offs?|benchmarks?|success criteri\w+|pass marks?|hurdles?|minimums?|"
           r"(?:gate|validation|sign-?up|conversion|waitlist|pre-?order|evidence) (?:goals?|criteri\w+))")
_add("gate_tampering", "r",
     r"\b(?:lower|reduce|drop|change|edit|adjust|move|tweak|relax|loosen|rewrite|modify|alter|update|redefine|fudge|bump down|cut|shift|"
     r"soften|reset)\w*\s+" + _g(4) + _TARGET + r"\b.{0,120}?"
     r"(?:\bafter\b|now that|\bsince\b|after the fact|retroactive\w*|so (?:that )?(?:it|this|we|our|the \w+) (?:pass|meet|count|qualif)\w*|"
     r"to (?:pass|clear|meet|match|make (?:it|the cut|this))\b|clears? the (?:bar|gate|target|threshold)|counts? as (?:a )?(?:go|pass)|"
     r"(?:mark|call) it (?:a )?(?:go|pass)|results?\b|passe[sd]\b|\bgo\b)",
     unless=r"\bbefore (?:we|the (?:step|test|survey|run)|it|running|starting|launch)|"
            r"target (?:price|audience|market|customers?|users|group|segment|persona|reader|buyer|date)")
_add("gate_tampering", "r",
     r"\b(?:mark|record|log|set|flip|change|switch|report|write|save|label)\w*\s+" + _g(4) +
     r"(?:gate|decision|outcome|result|stop|verdict)\w*\s+" + _g(3) + r"(?:as|to)\s+(?:a\s+)?(?:go|pass\w*|green|success\w*|approved)\b")
_add("gate_tampering", "r",
     r"\b(?:override|overrule|reverse|flip|overturn|ignore|change|hide|delete|erase)\w*\s+" + _g(3) +
     r"(?:stop|gate|pivot)\w*\s*(?:decision|outcome|recommendation|memo|result)?\b.{0,80}?"
     r"(?:won't notice|will not notice|wouldn't notice|without (?:telling|asking|the founder)|don't tell|do not tell|secretly|quietly|silently|nobody will know)", neg=False)
_add("gate_tampering", "r",
     r"\b(?:results?|numbers|data|it)\s+(?:came back|came in|were|was|is|are|showed?|only|got)\b.{0,120}?"
     r"\b(?:redefin\w*|lower\w*|chang\w*|mov\w*|shift\w*|adjust\w*|call|count|treat|declare|mark|record)\s+" + _g(3) +
     r"(?:success|passing|the pass mark|what counts as|it|this|that|the (?:test|gate|result|step))\s+(?:as\s+)?(?:a\s+)?"
     r"(?:pass|go|success|win|passed|\d)", neg=False)
# Arabic
_AR_TARGET =r"(?:الهدف|هدف|الرقم المستهدف|المستهدف|الحد الادني|الحد|العتبه|التارجت|المعيار|نسبه النجاح)"
_add("gate_tampering", "r",
     r"(?:نزل|قلل|خفض|غير|عدل|ظبط|اخفض|نقلل|نغير|نعدل|ننزل|وطي)\S*\s+.{0,30}?" + _AR_TARGET + r".{0,80}?"
     r"(?:عشان|علشان|بعد|النتيجه|النتايج|نعدي|يعدي|تعدي|ننجح|نكمل|يبقي|go)")
_add("gate_tampering", "r",
     r"(?:خلي|اعتبر|سجل|اكتب|غير)\S*\s+.{0,30}?(?:البوابه|القرار|النتيجه|البوابات)\s+.{0,20}?(?:go|نجاح|ناجح|ناجحه|عدت|عدي|مقبول)")


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def _scan(text: str, ctx: str, categories: Optional[Tuple[str, ...]] = None) -> Optional[_Rule]:
    for rule in _RULES:
        if ctx not in rule.ctx:
            continue
        if categories is not None and rule.category not in categories:
            continue
        # Every match is examined: a negated early match must never hide a later real one.
        for m in rule.rx.finditer(text):
            if rule.neg and _negated(text, m.start()):
                continue
            if rule.unless is not None:
                window = text[max(0, m.start() - 40):min(len(text), m.end() + 40)]
                if rule.unless.search(window):
                    continue
            if rule.unless_before is not None:
                if rule.unless_before.search(text[max(0, m.start() - 40):m.start()]):
                    continue
            return rule
    return None


def _refuse(rule: _Rule) -> GuardrailResult:
    reason = _REASONS[rule.category]
    alt = _ALTERNATIVES[rule.category]
    if rule.category == "excluded_product" and rule.sub in _EXCLUDED_ALTERNATIVES:
        reason = _EXCLUDED_REASONS[rule.sub]
        alt = _EXCLUDED_ALTERNATIVES[rule.sub]
    return GuardrailResult(False, rule.category, reason, alt, "rules")


# Fail-closed result for input that is not text or cannot be processed. The category is
# None on purpose: no rule matched, so naming one of CATEGORIES would be a false record.
_UNCHECKABLE = GuardrailResult(
    False,
    None,
    "Sorry, this input is not plain text or could not be checked safely, so it was not passed on.",
    "Honest alternative: please send the request as plain text and try again.",
    "rules",
)


def _run(text, passes) -> GuardrailResult:
    try:
        raw, is_text = _coerce(text)
        norm = _normalize(raw)
        if norm:
            for ctx, cats in passes:
                rule = _scan(norm, ctx, cats)
                if rule is not None:
                    return _refuse(rule)
        return _ALLOWED if is_text else _UNCHECKABLE
    except Exception:
        return _UNCHECKABLE


_NON_EXCLUDED = tuple(c for c in CATEGORIES if c != "excluded_product")


def check_request(text: str) -> GuardrailResult:
    """Check a founder/user request before any agent acts on it."""
    return _run(text, (("r", _NON_EXCLUDED), ("r", ("excluded_product",))))


def check_output(text: str) -> GuardrailResult:
    """Check agent-produced content before it is saved or shown."""
    return _run(text, (("o", _NON_EXCLUDED), ("o", ("excluded_product",))))


def check_brief(text: str) -> GuardrailResult:
    """Check the founder brief (idea + notes + target market as one string).

    An excluded product is reported first, with a compliant digital reframing where one exists.
    """
    return _run(text, (("r", ("excluded_product",)), ("r", _NON_EXCLUDED)))


# ==========================================================================
# Landing page check (M3 section 2.3)
# ==========================================================================
# Two extra refusal kinds that only a landing page can have. They are not request
# categories, so they are kept apart from CATEGORIES.
LANDING_CATEGORIES = ("external_script_or_tracker", "unlabelled_mock_form")
MOCK_FORM_LABEL = "MOCKED — demo form"

_LP_REASONS = {
    "external_script_or_tracker": (
        "Sorry, this page can't be used as it is: it loads an outside script, tracker, frame or "
        "file, or sends form data somewhere, and the landing page must be self-contained and track nobody."
    ),
    "unlabelled_mock_form": (
        "Sorry, this page can't be used as it is: its form is a demo that submits nowhere, so it must "
        "show the visible label \"MOCKED — demo form\"."
    ),
    "fake_social_proof": (
        "Sorry, this page can't be used as it is: it shows testimonials, reviews, star ratings or "
        "numbers of people that are not real evidence."
    ),
    "fake_urgency": (
        "Sorry, this page can't be used as it is: it has a countdown, a timer or an \"only X left\" claim."
    ),
    "impersonation": (
        "Sorry, this page can't be used as it is: it speaks in the founder's own first-person voice, "
        "or presents another brand as a partner, without the founder's approval."
    ),
}
_LP_ALTERNATIVES = {
    "external_script_or_tracker": (
        "Honest alternative: keep all CSS inline, remove outside scripts, frames and trackers, and keep "
        "the form with no action (it is MOCKED)."
    ),
    "unlabelled_mock_form": (
        "Honest alternative: add a visible line \"MOCKED — demo form\" next to the form."
    ),
    "fake_social_proof": (
        "Honest alternative: use the honest status line (\"In development — join the waitlist to hear "
        "when it's ready\") and no testimonials, ratings or counts."
    ),
    "fake_urgency": (
        "Honest alternative: remove the timer and the scarcity claim; a waitlist needs no deadline."
    ),
    "impersonation": (
        "Honest alternative: write the page in a neutral product voice and name no other brand as a "
        "partner; the founder can add a personal note after approving it."
    ),
}

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
              "track", "wbr"}
_NON_VISIBLE = {"script", "style", "noscript", "template", "head"}
_OUTSIDE = re.compile(r"^\s*(?:https?:)?//", re.I)


class _PageParser:
    """Collects visible text, hidden text, script code and risky tags (html.parser, stdlib)."""

    def __init__(self, markup: str):
        from html.parser import HTMLParser

        outer = self
        self.visible, self.hidden, self.scripts, self.attr_text = [], [], [], []
        self.external, self.forms, self.title = [], 0, []
        self._stack = []   # (tag, hides)

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                outer._start(tag, {k.lower(): (v or "") for k, v in attrs})
                if tag in _VOID_TAGS:
                    outer._end(tag)

            def handle_startendtag(self, tag, attrs):
                outer._start(tag, {k.lower(): (v or "") for k, v in attrs})
                outer._end(tag)

            def handle_endtag(self, tag):
                outer._end(tag)

            def handle_data(self, data):
                outer._data(data)

        parser = P(convert_charrefs=True)
        parser.feed(markup)
        parser.close()

    def _hidden_now(self) -> bool:
        return any(h for _, h in self._stack)

    def _in(self, *tags) -> bool:
        return any(t in tags for t, _ in self._stack)

    def _start(self, tag, a):
        style = re.sub(r"\s+", "", a.get("style", "").lower())
        hides = ("hidden" in a or "display:none" in style or "visibility:hidden" in style
                 or a.get("aria-hidden", "").lower() == "true" or tag in _NON_VISIBLE)
        self._stack.append((tag, hides))
        for key, value in a.items():
            if key.startswith("on"):
                self.scripts.append(value)            # inline event handlers are script code
            elif key in ("alt", "title", "placeholder", "value", "aria-label", "content", "label"):
                self.attr_text.append(value)
            if key in ("class", "id") and re.search(r"count-?down|timer|testimonial|review|rating|stars?\b", value, re.I):
                self.attr_text.append(re.sub(r"[-_]", " ", value))
            if key == "href" and value.strip().lower().startswith("javascript:"):
                self.scripts.append(value)
        src = a.get("src", "")
        if tag in ("script", "iframe", "frame", "embed", "object"):
            if src or a.get("data", "") or tag != "script":
                self.external.append(f"<{tag}> {src or a.get('data', '') or 'element'}")
        if tag == "link" and _OUTSIDE.match(a.get("href", "")):
            self.external.append(f"<link> loads {a.get('href')}")
        if tag in ("img", "source", "video", "audio", "input") and _OUTSIDE.match(src):
            self.external.append(f"<{tag}> loads {src}")
        if tag == "form":
            self.forms += 1
            action = a.get("action", "").strip()
            if action and not action.startswith("#") and not action.lower().startswith("javascript:"):
                self.external.append(f"<form> submits to {action}")
        if tag in ("button", "input") and a.get("formaction", "").strip() and not a["formaction"].strip().startswith("#"):
            self.external.append(f"a form button submits to {a['formaction']}")
        if tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            self.external.append("<meta http-equiv=refresh>")

    def _end(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return

    def _data(self, data):
        if self._in("script"):
            self.scripts.append(data)
        elif self._in("style"):
            if re.search(r"@import|url\(\s*['\"]?(?:https?:)?//", data, re.I):
                self.external.append("CSS loads an outside file")
        elif self._in("title"):
            self.title.append(data)
        elif self._hidden_now():
            self.hidden.append(data)
        else:
            self.visible.append(data)


_TRACKERS = re.compile(
    r"google-?analytics|googletagmanager|gtag\s*\(|\bga\s*\(\s*['\"]|fbq\s*\(|facebook\.net|connect\.facebook|"
    r"hotjar|mixpanel|segment\.(?:com|io)|analytics\.js|clarity\.ms|doubleclick|plausible\.io|matomo|piwik|"
    r"amplitude|heapanalytics|fullstory|intercom|snap\.licdn|linkedin\.com/px|"
    r"navigator\.sendBeacon|XMLHttpRequest|\bfetch\s*\(|\bWebSocket\s*\(|\bimport\s*\(|document\.cookie|localStorage|"
    r"new\s+Image\s*\(|\.src\s*=|document\.write|\beval\s*\(|https?://", re.I)
_TIMER_JS = re.compile(r"\bset(?:Interval|Timeout)\s*\(|requestAnimationFrame|count-?down|timeLeft|remaining(?:Time|Seconds)|"
                       r"getTime\s*\(\)\s*-|Date\.now\s*\(\)\s*-", re.I)
_LP_PROOF = re.compile(
    r"[★⭐✩✪✫✬✭✮✯☆]|\btestimonials?\b|\b(?:customer|user|client|buyer|member|student)s?'? (?:reviews?|stories|quotes?|"
    r"feedback|love)\b|\breviews?\b\s*(?:[:(]|from\b)|\b(?:rated|rating|ratings)\b|\b(?:5|five|4\.\d)[- ]?stars?\b|"
    r"\bwhat (?:our |my )?(?:customers|users|clients|people|founders|members|students|readers) (?:are saying|say|think)\b|"
    r"\b(?:loved|trusted|used|chosen|backed) by\b|\bwall of love\b|\bhappy (?:customers|users|clients)\b|"
    r"\bjoin\s+(?:over\s+|more than\s+|the\s+)?\d|\bjoin\s+(?:thousands|hundreds|dozens|millions)\b|"
    r"\b\d[\d,.]*\s?(?:k|m|thousand|million)?\+?\s+" + _PEOPLE + r"\b|"
    r"\b\d[\d,.]*\s?%|\b\d+(?:\.\d+)?\s?x\s+(?:more|faster|better|growth|revenue|sales)\b|#\s?1\b|\bnumber one\b", re.I)
_LP_URGENCY = re.compile(
    r"\bcount-?down\b|\btimer\b|\b\d{1,2}\s*:\s*\d{2}\s*:\s*\d{2}\b|\b(?:only|just)\s+\d+\s+\S+\s+(?:left|remaining)\b|"
    r"\b(?:only|just)\s+(?:a few|few|\d+)\s+(?:left|remaining|spots|seats|places|copies)\b|"
    r"\b(?:spots?|seats?|places|copies)\s+(?:left|remaining)\b|\b(?:ends?|closes?|expires?)\s+(?:in|tonight|today|soon|at midnight)\b|"
    r"\bhurry\b|\blimited[- ](?:time|spots|offer|availability)\b|\blast chance\b|\bact (?:now|fast)\b|\bdon'?t miss\b|"
    r"\bselling fast\b|\bbefore it'?s (?:gone|too late)\b|\b\d+\s*(?:h|hrs?|hours?)\s+(?:left|to go|remaining)\b", re.I)
_LP_INCOME = re.compile(
    r"\bearn\s+(?:up to\s+|over\s+|an extra\s+)?\$|\$\s?\d[\d,.]*\s?k?\s*(?:/|per|a|an|every)\s*(?:month|mo|week|day|year|hour|hr)\b|"
    r"\bguarantee\w*\b|\b(?:double|triple|10x|2x|3x)\s+your\b|\b(?:make|earn|bank)\s+(?:money|\$|thousands|six|passive income)|"
    r"\bpassive income\b|\bget rich\b|\bfinancial freedom\b|\bquit your\b|\bresults in \d+\s*days\b|"
    r"\byou(?:'ll| will)\s+(?:definitely\s+|certainly\s+)?(?:earn|make|get|see|double|grow|save)\s+(?:\$|\d|more|your)", re.I)
_LP_FOUNDER_VOICE = re.compile(
    r"\bi(?:'m| am|'ve| have|'ll| will)?\s+(?:built|building|created|creating|made|making|launching|launched|started|"
    r"designed|wrote|founded|developing|developed|working on)\b|\b(?:hi|hello|hey)[,!]?\s+i(?:'m| am)\b|\bmy name is\b|"
    r"\b(?:a )?note from (?:me|the founder)\b|\bfounder'?s note\b|\bfrom the founder\b|"
    r"[-—–]\s*\w+(?:\s+\w\.?)?,\s*(?:founder|co-?founder|ceo|creator|maker)\b|"
    r"\bmy (?:product|app|startup|company|tool|template|course|newsletter)\b|\bi personally\b|\bi promise\b|"
    r"\bwhy i (?:built|made|created|started)\b", re.I)
_LP_PARTNER = re.compile(
    r"\b(?:partner(?:ed|s|ship)?|official(?:ly)?|endorsed|sponsored|backed|approved|certified|featured|as seen|"
    r"in collaboration|in association|affiliated|trusted|recommended)\b.{0,40}?\b" + _BRANDS + r"\b|"
    r"\b" + _BRANDS + r"\b.{0,25}?\b(?:partner(?:ed|s|ship)?|endorse[sd]?|sponsor(?:ed|s)?|approved|certified|recommends?)\b|"
    r"\bour (?:partners|sponsors|investors|clients)\b", re.I)


def _lp_refuse(kind: str, detail: str = "") -> GuardrailResult:
    reason = _LP_REASONS.get(kind, _REASONS.get(kind, ""))
    alt = _LP_ALTERNATIVES.get(kind, _ALTERNATIVES.get(kind))
    if detail:
        reason += f" (found: {' '.join(detail.split())[:80]})"
    return GuardrailResult(False, kind, reason, alt, "rules")


def check_landing_page(html: str) -> GuardrailResult:
    """Check a generated landing page before it is saved, shown or published.

    Blocks: testimonials, reviews or stars; "join N others" and other counts or percentages;
    countdowns, timers and "only X left" (in the text or in script code); income or results
    promises; other brands presented as partners; the founder's first-person voice; outside
    scripts, trackers, frames, files or form targets. Any form needs the visible label
    "MOCKED — demo form". The page's text and code also go through ``check_output``.
    The category is one of CATEGORIES or LANDING_CATEGORIES.
    """
    try:
        if not isinstance(html, (str, bytes, bytearray)):
            return _UNCHECKABLE
        markup = html.decode("utf-8", "replace") if isinstance(html, (bytes, bytearray)) else html
        page = _PageParser(markup)
        visible = " ".join(" ".join(page.title + page.visible).split())
        everything = " ".join([visible, " ".join(page.hidden), " ".join(page.attr_text)])
        code = "\n".join(page.scripts)
        if page.external:
            return _lp_refuse("external_script_or_tracker", page.external[0])
        m = _TRACKERS.search(code)
        if m:
            return _lp_refuse("external_script_or_tracker", m.group(0))
        m = _TIMER_JS.search(code)
        if m:
            return _lp_refuse("fake_urgency", m.group(0))
        for rx, kind in ((_LP_PROOF, "fake_social_proof"), (_LP_URGENCY, "fake_urgency"),
                         (_LP_INCOME, "income_promise"), (_LP_PARTNER, "impersonation"),
                         (_LP_FOUNDER_VOICE, "impersonation")):
            m = rx.search(everything)
            if m:
                return _lp_refuse(kind, m.group(0))
        general = check_output(everything + "\n" + code)
        if not general.allowed:
            return general
        if page.forms and not ("MOCKED" in visible and _normalize(MOCK_FORM_LABEL) in _normalize(visible)):
            return _lp_refuse("unlabelled_mock_form")
        return _ALLOWED
    except Exception:
        return _UNCHECKABLE


# ==========================================================================
# Layer 2: optional local-LLM classifier (M5 section 1)
# ==========================================================================
# It runs only when Layer 1 allows the text; a block from either layer is a block.
# The model client is passed in by the caller (dependency injection): this module
# never imports the model client and no setting or environment variable turns it on or off.
CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES) + ["none"]},
        "allowed": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["category", "allowed", "confidence", "reason"],
}
CLASSIFIER_PROMPT_PATH = "prompts/guardrail.md"
_SIDE_EFFECT_HINT = re.compile(
    r"\b(?:publish\w*|post|posting|send\w*|email\w*|e-mail|tweet\w*|launch\w*|go live|deploy\w*|announce\w*|dm\b|message\w*|"
    r"spend\w*|pay|paying|payment|buy\w*|purchas\w*|charg\w*|refund\w*|transfer\w*|wire|sign|signing|signed|submit\w*|"
    r"upload\w*|run (?:the )?ads?)\b|"
    + _ar(r"انشر|نشر|ابعت|ارسل|ادفع|اشتري|اصرف|حول|رجع|وقع|بوست|ايميل"), re.I)

_CLASSIFIER_HOLD = GuardrailResult(
    False,
    None,
    "Sorry, the safety classifier is not available right now, and this request would publish, send, "
    "spend or sign something, so it was held back for the founder to decide.",
    "Honest alternative: the founder can review the request and approve it at the approval prompt, "
    "or try again when the local model is running.",
    "classifier_unavailable",
)


def is_side_effect_request(text) -> bool:
    """True when the request reads like a publish/send/spend/sign action (English or Arabic)."""
    try:
        return bool(_SIDE_EFFECT_HINT.search(_normalize(_coerce(text)[0])))
    except Exception:
        return True   # unsure: treat it as a side effect (the safer side)


def _classifier_prompt(text: str) -> tuple:
    from pathlib import Path

    system = (Path(__file__).resolve().parent / CLASSIFIER_PROMPT_PATH).read_text(encoding="utf-8")
    body = str(text).replace("<<<REQUEST", "< < <REQUEST").replace("REQUEST>>>", "REQUEST> > >")
    return system, f"Classify this request.\n\n<<<REQUEST\n{body[:6000]}\nREQUEST>>>"


def _parse_verdict(reply) -> dict:
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict):
        raise ValueError("the classifier reply has no JSON object")
    category, allowed, confidence = data.get("category"), data.get("allowed"), data.get("confidence")
    if category not in CATEGORIES and category != "none":
        raise ValueError(f"unknown category {category!r}")
    if not isinstance(allowed, bool):
        raise ValueError("'allowed' is not true/false")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("'confidence' is not a number from 0 to 1")
    if allowed and category != "none":
        raise ValueError("inconsistent reply: allowed with a category")
    return data


def check_request_layered(text, llm_client=None, side_effect: Optional[bool] = None) -> GuardrailResult:
    """Layer 1 rules, then (only if they allow it) the local-LLM classifier.

    ``llm_client`` has ``generate_json(prompt, schema, system=None, name=None)``; None means
    Layer 1 only (``note`` says so). ``side_effect`` says whether the request would publish,
    send, spend or sign; None means it is guessed from the text.
    Fail safe when the classifier errors: a side-effect request is held for the founder
    (layer "classifier_unavailable"); a plain content request keeps the Layer 1 result
    and ``note`` says the classifier was unavailable, for the caller to log.
    """
    first = check_request(text)
    if not first.allowed:
        return first
    if llm_client is None:
        return GuardrailResult(True, None, "", None, None, "classifier not configured: Layer 1 only")
    try:
        system, prompt = _classifier_prompt(_coerce(text)[0])
        verdict = _parse_verdict(llm_client.generate_json(prompt, CLASSIFIER_SCHEMA, system=system,
                                                          name="guardrail.classify"))
    except Exception as e:
        note = f"classifier unavailable ({type(e).__name__}: {str(e)[:160]})"
        held = is_side_effect_request(text) if side_effect is None else bool(side_effect)
        if held:
            return GuardrailResult(_CLASSIFIER_HOLD.allowed, None, _CLASSIFIER_HOLD.reason,
                                   _CLASSIFIER_HOLD.safe_alternative, "classifier_unavailable",
                                   note + "; side-effect request held for the founder")
        return GuardrailResult(True, None, "", None, None, note + "; Layer 1 only")
    model_reason = " ".join(str(verdict.get("reason", "")).split())[:200]
    if verdict["allowed"]:
        return GuardrailResult(True, None, "", None, None, f"classifier allowed (confidence {verdict['confidence']:g})")
    category = verdict["category"] if verdict["category"] in CATEGORIES else None
    reason = _REASONS.get(category) or ("Sorry, I can't help with this one: the safety classifier found that it "
                                        "breaks one of this system's honesty or safety rules.")
    alternative = _ALTERNATIVES.get(category) or ("Honest alternative: rephrase the request so that it stays "
                                                  "within the project's rules, or ask the founder to review it.")
    return GuardrailResult(False, category, reason, alternative, "classifier",
                           f"classifier blocked (confidence {verdict['confidence']:g}): {model_reason}")
