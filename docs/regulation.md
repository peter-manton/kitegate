# Deploying for children: standards and law

Notes for anyone deploying Kitegate to children, especially in the UK.
Checked October 2026. None of this is legal advice: take legal advice before
any public or school deployment.

## Distress and safeguarding

- The child-facing distress reply always offers a route that does not depend
  on the pack's trusted adult (another grown-up, and Childline on 0800 1111),
  because that adult may be the problem. This is fixed in the core.
- Decide in advance who is told about a disclosure, and write it down. In a
  family deployment that is usually the account holder. In a school it is
  the Designated Safeguarding Lead, never the parent first. A hosted service
  needs a published crisis protocol; the DfE standards expect one.
- Only gateway deployments can alert anyone (the gateway's block log and
  alert webhook). The single-file Open WebUI filter cannot.

## Filtering expectations

The deterministic core is a fast, predictable floor with published
false-positive fixtures. On its own it does not meet the DfE filtering
expectations, which include misspellings, abbreviations, other languages,
images and conversational context. Add a classifier layer (the gateway's
optional NeMo Guardrails stage, with a self-hosted NemoGuard model) and keep
image features off, or screen images, before deploying to a school.

## UK schools

The DfE's *Generative AI: product safety standards* (updated January 2026)
are the procurement checklist. Points that affect configuration:

- **No persona.** Avoid "I" statements and character names. The `uk-school`
  pack's replies and framing are written this way, and a test enforces it.
  Separately, and in every pack, the core tells the model it is not a person,
  a friend or a romantic interest, and screens the reply for it on the way out
  (tier 4, `persona.*`). That covers the companion-style interaction the
  standards are aimed at; the `uk-school` pack goes further on house style.
- **No singling out of protected characteristics.** A tool that redirects
  one protected characteristic will fail a school's equality review. The
  `uk-school` pack redirects party-political opinion, romance, puberty and
  mortality questions to a teacher, and nothing else.
- **Hints before answers for school work.** The `uk-school` framing asks for
  a hint or first step before a full answer.
- **Time limits, reports and safeguarding lead details** are features of the
  service in front of Kitegate, not of this package.
- **Accessibility, DPIA, data processing agreement and retention** likewise
  belong to the deploying service.

Schools also check providers against the UK Safer Internet Centre's
appropriate filtering and monitoring definitions, whose 2026 edition covers
AI-generated content.

## Data protection and online safety

- **ICO Children's Code.** Applies to services likely to be accessed by
  children: a DPIA, data minimisation and high-privacy defaults.
- **Online Safety Act.** The government has taken powers to bring chatbots
  under the illegal content duties and has committed to mandatory breaks for
  under-18s. Plan to be in scope, and keep sharing features out.
- **COPPA (US).** The amended rule has required full compliance since
  22 April 2026, including separate parental consent for using children's
  data to train AI. A children's encyclopedia is child-directed. A UK-only
  service that blocks US users avoids COPPA, FERPA and state student-privacy
  law.
- **Model providers.** A promise that children's data is never used for
  training only holds if the inference provider honours it. Use providers
  with zero data retention and a signed DPA, name them as subprocessors, and
  document any international transfer. Do not send children's messages to
  evaluation endpoints such as NVIDIA's build.nvidia.com.

## Sources

- [DfE: Generative AI product safety standards](https://www.gov.uk/government/publications/generative-ai-product-safety-standards/generative-ai-product-safety-standards)
- [Government response, Growing up in the online world (July 2026)](https://www.gov.uk/government/consultations/growing-up-in-the-online-world-a-national-consultation/outcome/growing-up-in-the-online-world-government-response-july-2026)
- [PM announcement on AI chatbots and the OSA (Feb 2026)](https://www.gov.uk/government/news/pm-no-platform-gets-a-free-pass-government-takes-action-to-keep-children-safe-online)
- [COPPA compliance dates (Davis Polk)](https://www.davispolk.com/insights/client-update/ftc-prioritizes-coppa-enforcement-new-compliance-obligations-take-effect)
- [FTC age verification policy statement (Feb 2026)](https://www.ftc.gov/news-events/news/press-releases/2026/02/ftc-issues-coppa-policy-statement-incentivize-use-age-verification-technologies-protect-children)
- [UK SIC: KCSIE 2026 filtering and monitoring](https://saferinternet.org.uk/blog/important-updates-to-keeping-children-safe-in-education-2026)

Content from these sources is paraphrased.
