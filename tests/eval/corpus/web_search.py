"""Web search domain of the expanded eval corpus.

One hundred twenty-eight background entries, sixteen version pairs, forty
answerable queries and twelve unanswered queries, on the web search work the
fleet runs (see README.md for the rules).
"""

from __future__ import annotations

from tests.eval.domains import Domain
from tests.eval.version_pairs import VersionDomain, VersionPair

DOMAIN: Domain = Domain(
    "web search",
    (
        (
            "fact",
            "The search console property is a domain property verified with a dns txt record, so it survives "
            "a domain move",
            ("search-console", "verification"),
            "A URL-prefix property was tried first and lost its history when the site moved from the www "
            "host to the apex. The domain property aggregates every protocol and subdomain, which is why the "
            "DNS record route was chosen.",
        ),
        (
            "fact",
            "The webmaster tools account for bing is linked to the same domain with a dns verification on "
            "the host",
            ("webmaster", "bing"),
            None,
        ),
        (
            "fact",
            "The robots file disallows the checkout and account paths because the session token sits in the "
            "url",
            ("robots", "crawl"),
            None,
        ),
        (
            "fact",
            "The xml sitemap is an index of five files, each under the 50000 url limit the engines impose",
            ("sitemap", "xml"),
            "The first sitemap was a single file that crossed the limit and was silently truncated in the "
            "console report. Splitting by content type (products, categories, articles, authors, static) "
            "made each file's submitted-versus-indexed count readable.",
        ),
        (
            "fact",
            "The crawl budget for the property is about 20000 urls a day, measured from the console crawl "
            "stats",
            ("crawl", "budget"),
            "The figure is the average of the crawl stats report over the last 90 days, not a count from "
            "server logs. It stops holding during a migration, when requests spike for several weeks.",
        ),
        (
            "fact",
            "The index coverage sits at 80 percent, the excluded 20 percent being thin and "
            "canonical-duplicate pages",
            ("index", "coverage"),
            "The excluded 20 percent is mostly 'Crawled - currently not indexed' and 'Duplicate, Google "
            "chose different canonical' rows in the page indexing report. Adding noindex to the thin pages "
            "moved the number only slightly, because they were already being left out.",
        ),
        (
            "fact",
            "Every page carries a self-referencing canonical; the faceted pages point at the canonical "
            "facet-less url",
            ("canonical", "seo"),
            None,
        ),
        (
            "fact",
            "The hreflang set covers five languages with x-default pointing at the english version",
            ("hreflang", "international"),
            None,
        ),
        (
            "fact",
            "Core web vitals on the landing pages are lcp 2.4 s, cls 0.08, inp 180 ms, all inside the "
            "thresholds",
            ("core-web-vitals", "performance"),
            "Figures are field data at the 75th percentile from the Chrome UX Report, not lab runs. "
            "Lighthouse scores for the same pages were consistently worse and were not used for the "
            "thresholds.",
        ),
        (
            "fact",
            "The lcp on the product pages was 4.1 s until the hero image moved to avif and preloaded",
            ("lcp", "performance"),
            "Converting the hero image to AVIF alone only brought LCP down to 3.3 s. The rest came from "
            "adding a preload hint with fetchpriority high, since the browser had been discovering the image "
            "late.",
        ),
        (
            "fact",
            "The cls on the article pages came from the late-loading ad slot; reserving the slot height "
            "fixed it",
            ("cls", "performance"),
            "The ad slot collapsed to zero height until the bidder responded, pushing the article text down. "
            "Setting a min-height for the tallest creative removed the shift, whereas lazy-loading the slot "
            "made no difference.",
        ),
        (
            "fact",
            "The inp on the search results page was the typeahead debounce; moving it to the server cut it "
            "in half",
            ("inp", "performance"),
            "Profiling in Chrome DevTools showed the main thread blocked on filtering suggestions after "
            "every keystroke. Raising the debounce interval did nothing useful until the matching moved "
            "server side.",
        ),
        (
            "fact",
            "The structured data on the product pages is json-ld with the product, offer and aggregateRating "
            "types",
            ("schema", "structured-data"),
            None,
        ),
        (
            "fact",
            "The faq schema on the category pages uses the faqpage type with ten questions each",
            ("faq", "schema"),
            None,
        ),
        (
            "fact",
            "The breadcrumb markup on every page is the breadcrumblist type with the position and name "
            "fields",
            ("breadcrumb", "schema"),
            None,
        ),
        (
            "fact",
            "The article pages use the article schema with the headline, author and datePublished fields",
            ("article", "schema"),
            None,
        ),
        (
            "fact",
            "Title tags stay under 60 characters with the primary keyword near the front and the brand at "
            "the end",
            ("title", "seo"),
            None,
        ),
        (
            "fact",
            "Meta descriptions run to 155 characters; the ones with a question phrasing click 12 percent "
            "better",
            ("meta", "seo"),
            "The 12 percent comes from a Search Console comparison of 200 pages with question-style "
            "descriptions against 200 matched pages without. Google rewrites many descriptions anyway, so "
            "the gain is smaller where the snippet is replaced.",
        ),
        (
            "fact",
            "Each page has exactly one h1 matching the page title's primary keyword",
            ("h1", "seo"),
            None,
        ),
        (
            "fact",
            "Internal links per page average 20 with descriptive anchors; the navigation links are excluded "
            "from the count",
            ("internal-links", "seo"),
            None,
        ),
        (
            "fact",
            "The referring domain count is 5200; the top 100 domains contribute 60 percent of the link "
            "equity",
            ("backlinks", "off-page"),
            "The count is a deduplicated total from the backlink tool, merging subdomains of the same "
            "registered domain. Equity share was estimated from the tool's page-level rating, so it is a "
            "rough guide only.",
        ),
        (
            "fact",
            "The domain rating is 41; the jump from 33 to 41 tracked the digital pr campaign, not the "
            "content",
            ("domain-rating", "off-page"),
            "Content publishing over the same months added little to the referring domain count, while the "
            "PR placements added most of the new domains. The gain holds only while those links stay live, "
            "and two of the larger placements were later removed.",
        ),
        (
            "fact",
            "Keyword difficulty on the money pages averages 34; the cluster pages sit at 18",
            ("keyword-difficulty", "seo"),
            None,
        ),
        (
            "fact",
            "Monthly search volume on the tracked keyword set is 48000, up 9 percent year on year",
            ("search-volume", "seo"),
            None,
        ),
        (
            "fact",
            "Serp features on the money queries: ai overviews on 45 percent, featured snippets on 18, people "
            "also ask on 30",
            ("serp", "features"),
            "Shares come from a weekly SERP pull on the 500 tracked money queries, so they swing whenever "
            "Google changes which queries trigger a feature. The AI overview share was 31 percent at the "
            "first pull and rose steadily afterwards.",
        ),
        (
            "fact",
            "The ai overview appears on 45 percent of the tracked queries and cites the site on 9 percent of "
            "them",
            ("ai-overview", "serp"),
            "A citation is counted only when the site's domain appears in the overview's source cards, not "
            "when the brand is mentioned in the text. Pages with a concise answer paragraph at the top were "
            "cited more often than long guides.",
        ),
        (
            "fact",
            "The featured snippet capture rate is 18 percent on the informational set, up from 11 after the "
            "list restructure",
            ("featured-snippet", "serp"),
            None,
        ),
        (
            "fact",
            "The knowledge panel shows on 30 percent of the brand queries with the entity facts from the "
            "structured data",
            ("knowledge-panel", "serp"),
            None,
        ),
        (
            "fact",
            "The people also ask block carries four follow-ups on 30 percent of the informational queries",
            ("paa", "serp"),
            None,
        ),
        (
            "fact",
            "The local pack shows three businesses; the site holds slot two in 40 percent of the local query "
            "set",
            ("local-pack", "local-seo"),
            None,
        ),
        (
            "fact",
            "The google business profile has 214 reviews averaging 4.6, with the photos refreshed monthly",
            ("google-business-profile", "local-seo"),
            None,
        ),
        (
            "fact",
            "The name address phone citation is consistent across the 28 directory listings; three drifted "
            "and were fixed",
            ("nap", "local-seo"),
            None,
        ),
        (
            "fact",
            "Review responses go out within 48 hours; the negative ones get a longer, more specific reply",
            ("review", "local-seo"),
            None,
        ),
        (
            "fact",
            "The citation building covers 28 directories, the national ones first and the local ones in the "
            "second pass",
            ("citation", "local-seo"),
            None,
        ),
        (
            "fact",
            "Mobile first indexing applies to the whole site; the tap targets on the footer were enlarged to "
            "44 px",
            ("mobile-first", "index"),
            None,
        ),
        (
            "fact",
            "The product pages render with server side rendering; the client hydration happens after the "
            "first paint",
            ("javascript", "rendering"),
            "Client-only rendering was left in place for the account area, which is blocked from crawling "
            "anyway. Hydration errors appeared when a personalised banner differed between server and "
            "browser, and were fixed by rendering it after mount.",
        ),
        (
            "fact",
            "The prerendered pages are the 120 highest-traffic urls, refreshed hourly",
            ("prerender", "rendering"),
            "Prerendered pages are served from cache to crawlers, while users get the normal server render. "
            "The list is rebuilt from the top organic landing pages, and an entry is evicted when its "
            "product goes out of stock.",
        ),
        (
            "fact",
            "The e-e-a-t pages carry an author box with the bio, the credentials and the photo, linked to "
            "the author archive",
            ("eeat", "quality"),
            None,
        ),
        (
            "fact",
            "The author archives list every article with the author's byline and a one-paragraph bio",
            ("author", "eeat"),
            None,
        ),
        (
            "fact",
            "The topical authority on the core cluster is 52 pages: 20 pillar, 32 cluster",
            ("topical-authority", "content"),
            None,
        ),
        (
            "fact",
            "The pillar pages run 2000 to 3000 words with a table of contents and the cluster links in the "
            "body",
            ("pillar", "content"),
            None,
        ),
        (
            "fact",
            "The cluster pages run 800 to 1200 words and link back to the pillar with the pillar's primary "
            "keyword",
            ("cluster", "content"),
            None,
        ),
        (
            "fact",
            "Content freshness: the money pages get a review every 90 days, the rest on a 6 month cycle",
            ("freshness", "content"),
            None,
        ),
        (
            "fact",
            "The evergreen pages are 40 percent of the index; they earn 55 percent of the organic sessions",
            ("evergreen", "content"),
            None,
        ),
        (
            "fact",
            "The query intent mix on the tracked set: 55 percent informational, 25 percent transactional, 12 "
            "percent commercial, 8 percent navigational",
            ("intent", "query"),
            "Intent was labelled by a rules classifier on query modifiers and checked against a "
            "hand-labelled sample of 300 queries, with 88 percent agreement. Queries with mixed intent were "
            "assigned to the stronger of the two.",
        ),
        (
            "fact",
            "The long tail is 90 percent of the tracked queries at under 10 searches a month, but it drives "
            "40 percent of the clicks",
            ("long-tail", "query"),
            "Most tail queries show zero or one click in a month, so the click share only appears when "
            "queries are grouped by topic. Rank changes on single tail queries are noise and do not raise "
            "alerts.",
        ),
        (
            "fact",
            "The head terms are 8 percent of the queries at over 1000 searches a month and 45 percent of the "
            "clicks",
            ("head-term", "query"),
            None,
        ),
        (
            "fact",
            "Keyword research runs on a 6000 query set refreshed monthly with the volume and difficulty from "
            "two tools",
            ("keyword-research", "seo"),
            None,
        ),
        (
            "fact",
            "Serp analysis on the money queries runs weekly; the feature mix is tracked per query, not per "
            "domain",
            ("serp-analysis", "seo"),
            None,
        ),
        (
            "fact",
            "Rank tracking covers 500 keywords in two locations across desktop and mobile, sampled daily",
            ("rank-tracking", "seo"),
            "The set was trimmed from 800 keywords after the tool price rose, and the dropped ones were "
            "low-value tail terms. Locations are London and Manchester, and mobile results differ from "
            "desktop on roughly a third of queries.",
        ),
        (
            "fact",
            "Average position on the money set is 6.2; the brand set is 1.4",
            ("position", "seo"),
            None,
        ),
        (
            "fact",
            "Impressions on the money set are 2.1 million a month with a 3.8 percent click through rate",
            ("impression", "seo"),
            None,
        ),
        (
            "fact",
            "Organic sessions are 82000 a month, 38 percent of total sessions",
            ("organic-traffic", "analytics"),
            None,
        ),
        (
            "fact",
            "The organic conversion rate is 2.9 percent, under the paid rate of 4.1",
            ("conversion", "analytics"),
            None,
        ),
        (
            "fact",
            "The bounce rate on the landing pages is 41 percent; the article pages are 33",
            ("bounce-rate", "analytics"),
            None,
        ),
        (
            "fact",
            "Time on page on the article pages averages 3 minutes 40 seconds",
            ("time-on-page", "analytics"),
            None,
        ),
        (
            "fact",
            "Scroll depth on the article pages reaches 70 percent for half the readers",
            ("scroll-depth", "analytics"),
            None,
        ),
        (
            "fact",
            "Page views are 310000 a month; the article pages are 40 percent of them",
            ("page-view", "analytics"),
            None,
        ),
        (
            "fact",
            "Campaign tagging uses the utm source, medium and campaign parameters; the naming scheme is in "
            "the style guide",
            ("utm", "tracking"),
            None,
        ),
        (
            "fact",
            "The ga4 property has two streams, web and mobile, with the consent mode v2 on both",
            ("ga4", "analytics"),
            None,
        ),
        (
            "fact",
            "The consent mode v2 is in granted mode for the eu traffic after the cookie banner, denied by "
            "default",
            ("consent-mode", "privacy"),
            "Consent mode was first deployed with analytics_storage granted by default, which an audit "
            "flagged as wrong for EU visitors. The default was changed to denied, and the banner updates the "
            "state through a consent update call.",
        ),
        (
            "fact",
            "The cookie banner is a two-button accept-all and reject-non-essential; the state is stored 12 "
            "months",
            ("cookie-banner", "privacy"),
            None,
        ),
        (
            "fact",
            "The consent management platform logs the consent state per user with the timestamp and the "
            "purpose list",
            ("cmp", "privacy"),
            None,
        ),
        (
            "fact",
            "The gdpr rights requests run through a single form; the deletion takes 30 days to propagate to "
            "the backups",
            ("gdpr", "privacy"),
            "The 30 days is set by the oldest nightly backup rotating out. A deletion is confirmed to the "
            "requester once the live tables are purged, with a note that backups clear within the month.",
        ),
        (
            "fact",
            "The ccpa do-not-sell flag is honoured in the ad server and the data partner list",
            ("ccpa", "privacy"),
            None,
        ),
        (
            "fact",
            "The first party data model joins the click stream with the account data on the hashed user id",
            ("first-party", "privacy"),
            None,
        ),
        (
            "fact",
            "The identity graph merges the anonymous and known profiles on the hashed id with a 90 day "
            "window",
            ("identity-graph", "privacy"),
            None,
        ),
        (
            "fact",
            "The cdp segments are built nightly; the activation list pushes to the ad server and the email "
            "platform",
            ("cdp", "privacy"),
            None,
        ),
        (
            "fact",
            "The dmp audience syncs to the ad server as a hashed id list, refreshed weekly",
            ("dmp", "privacy"),
            None,
        ),
        (
            "fact",
            "The attribution model is data driven; the last touch is kept as a secondary view for the "
            "reporting",
            ("attribution", "analytics"),
            None,
        ),
        (
            "fact",
            "The experiment platform runs a/b tests with the sequential testing option on for the "
            "long-running ones",
            ("ab-test", "experiment"),
            None,
        ),
        (
            "fact",
            "The minimum detectable effect for the landing page tests is 5 percent with the 80 percent power "
            "at 95 percent confidence",
            ("statistical-power", "experiment"),
            "The 5 percent minimum was chosen because the landing pages get about 40000 visitors a "
            "fortnight, so a smaller effect would need a month of traffic per test. Tests below that size "
            "are logged as inconclusive, not as no effect.",
        ),
        (
            "fact",
            "The a/b test on the checkout cut the drop-off by 4 percent; the lift was significant at the 99 "
            "percent level",
            ("lift", "experiment"),
            None,
        ),
        (
            "fact",
            "The incrementality test on the paid search showed a 12 percent lift over the holdout",
            ("incrementality", "experiment"),
            None,
        ),
        (
            "fact",
            "The causal inference on the campaign uses a synthetic control over the 90 day pre-period",
            ("causal", "experiment"),
            None,
        ),
        (
            "fact",
            "The core algorithm update tracking uses a pre and post comparison over the 14 day rollout "
            "window",
            ("algorithm-update", "seo"),
            "Comparisons use the same weekdays before and after to avoid weekend skew, and exclude queries "
            "with fewer than 100 impressions. Overlapping updates make the window unusable, so those periods "
            "are flagged and not analysed.",
        ),
        (
            "fact",
            "The spam update impact was measured on the thin content set; 8 percent of the urls lost "
            "indexing",
            ("spam-update", "seo"),
            None,
        ),
        (
            "fact",
            "The manual action was for scaled content abuse; the appeal removed the thin set and passed on "
            "the first try",
            ("manual-action", "seo"),
            "The notice cited auto-generated location pages published in bulk. The reviewer accepted the "
            "request after the pages were removed or merged into fewer, edited ones, and the notice cleared "
            "within about three weeks.",
        ),
        (
            "fact",
            "The appeal letter included the list of removed urls, the content policy and the sample of the "
            "rewritten pages",
            ("appeal", "seo"),
            None,
        ),
        (
            "fact",
            "The disavow file covers the 400 domains from the link audit; it is resubmitted after each major "
            "campaign",
            ("disavow", "seo"),
            None,
        ),
        (
            "fact",
            "The new site delay is about 6 weeks from first index to stable rankings on the money keywords",
            ("sandbox", "seo"),
            "Measured across the last four new subsites launched by the team, from first indexing to a "
            "stable top-20 position. A site with strong inbound links from launch took closer to 3 weeks, so "
            "the figure is a typical case, not a rule.",
        ),
        (
            "fact",
            "Rank volatility on the money set is a standard deviation of 4.2 positions over 30 days",
            ("rank-volatility", "seo"),
            None,
        ),
        (
            "fact",
            "Serp stability on the tracked set is 91 percent of queries keeping the same top 10 over a week",
            ("serp-stability", "seo"),
            None,
        ),
        (
            "fact",
            "The brand queries are 20 percent of the clicks at a 92 percent click through rate",
            ("brand-query", "seo"),
            None,
        ),
        (
            "fact",
            "The non brand queries are 80 percent of the clicks at a 2.1 percent click through rate",
            ("non-brand-query", "seo"),
            None,
        ),
        (
            "fact",
            "The organic to paid session split is 58 to 42; the organic share rose after the content program",
            ("organic-vs-paid", "analytics"),
            None,
        ),
        (
            "fact",
            "The seo to sem budget split is 60 to 40, rebalanced quarterly on the marginal return",
            ("seo-vs-sem", "analytics"),
            None,
        ),
        (
            "fact",
            "The blended reporting shows the position, the click and the conversion per query across organic "
            "and paid",
            ("blended", "analytics"),
            None,
        ),
        (
            "fact",
            "The serpapi account pulls 500 serps a day for the rank tracking and the feature tracking",
            ("serpapi", "api"),
            "The quota comes from the plan tier, and the daily job stops when it is used up. Rank tracking "
            "takes 400 of the calls and feature tracking the other 100, so adding keywords means dropping "
            "something else.",
        ),
        (
            "fact",
            "The ahrefs api pull is 5000 urls a day for the backlink and the difficulty metrics",
            ("ahrefs", "api"),
            None,
        ),
        (
            "fact",
            "The semrush api pull is 5000 urls a day for the position and the volume metrics",
            ("semrush", "api"),
            None,
        ),
        (
            "fact",
            "The screaming frog crawl runs weekly on the 50000 url cap with the 10 concurrency",
            ("screaming-frog", "crawl"),
            None,
        ),
        (
            "fact",
            "The technical crawl uses a headless browser with the 2 minute page timeout and the 50 "
            "concurrency",
            ("crawl", "technical"),
            "A 2 minute timeout was needed because the heaviest category templates take up to 90 seconds to "
            "settle when third-party scripts are slow. The crawl is paused if the origin returns more than 1 "
            "percent 5xx responses, to avoid adding load during an incident.",
        ),
        (
            "fact",
            "The render test compares the server html with the rendered dom; the diff on the product pages "
            "is under 2 percent",
            ("render", "technical"),
            None,
        ),
        (
            "fact",
            "The data pipeline is crawl, parse, store, rank and serve; the nightly run takes 3 hours",
            ("data-pipeline", "seo"),
            None,
        ),
        (
            "fact",
            "The cache on the static assets is 30 days with the immutable flag on the versioned files",
            ("cache", "performance"),
            None,
        ),
        (
            "fact",
            "The cdn serves the static assets from 28 edge locations with the 100 second purge",
            ("cdn", "performance"),
            None,
        ),
        (
            "fact",
            "The dedup pass clusters the near-duplicate urls with the 0.92 similarity threshold",
            ("dedup", "index"),
            "The threshold was picked by hand-labelling 150 URL pairs, where 0.92 separated true duplicates "
            "from templated near-misses with no false merges. Product pages differing only by colour sit "
            "just under it and are handled by canonical tags.",
        ),
        (
            "fact",
            "The cluster pass merges the parameter variants into the canonical url before the index update",
            ("cluster", "dedup"),
            None,
        ),
        (
            "fact",
            "The near-dup detection uses the shingled minhash with the 0.92 jaccard threshold",
            ("near-dup", "dedup"),
            None,
        ),
        (
            "fact",
            "The freshness decay on the news pages is a half life of 14 days in the ranking score",
            ("freshness", "ranking"),
            None,
        ),
        (
            "fact",
            "The quality score on the index is the e-e-a-t signal with the author and the source trust as "
            "the inputs",
            ("quality", "ranking"),
            None,
        ),
        (
            "fact",
            "The webspam filter drops the pages with the 30 percent keyword density and the 5 per paragraph "
            "ad density",
            ("webspam", "spam"),
            None,
        ),
        (
            "fact",
            "The link spam filter weights the anchor text concentration; the 80 percent exact match drops "
            "the page",
            ("link-spam", "webspam"),
            None,
        ),
        (
            "insight",
            "The ai overview cut the informational referral by 30 percent; moving the answer above the fold "
            "and adding the structured data recovered half of it",
            ("ai-overview", "referral"),
            "Clicks on informational queries fell once AI overviews began appearing for them, while branded "
            "and transactional queries were barely affected. Recovery was checked by comparing 30 days "
            "before and after the page changes, with queries that never showed an overview as the control.",
        ),
        (
            "insight",
            "The lcp regression on the landing pages came from a third party consent script; deferring it to "
            "after the fold cut the lcp by 1.2 s",
            ("lcp", "performance"),
            "Found by running WebPageTest with and without the consent script blocked, where LCP dropped by "
            "the same 1.2 s when it was removed. The banner now loads after the first paint and its space in "
            "the layout is reserved.",
        ),
        (
            "insight",
            "The index coverage drop to 62 percent traced to a redirect loop on the faceted pages; the loop "
            "was a 301 chain through the canonical",
            ("canonical", "index"),
            "A crawl of the faceted URLs showed 301 hops from the parameter URL to a canonical that itself "
            "redirected back. Fixing the canonical target brought coverage back to 80 percent within weeks, "
            "and the cause was spotted by sorting the report by redirect error.",
        ),
        (
            "decision",
            "The team chose server side rendering over client side rendering for the landing pages because "
            "the lcp on the client side build was 2.4 s slower",
            ("ssr", "rendering"),
            "Both builds were tested against the same 20 landing pages on a throttled mobile profile. Client "
            "rendering also delays content for crawlers that queue JavaScript, which was not measured "
            "directly but made the choice easier.",
        ),
        (
            "decision",
            "The team chose json-ld over microdata for the structured data because the json-ld kept the html "
            "clean and the validation tooling was better",
            ("jsonld", "schema"),
            "Microdata was already present on a few legacy templates and clashed with the JSON-LD where both "
            "existed. Those templates were converted so that only one format remains per page.",
        ),
        (
            "fact",
            "The zero click rate on the informational set is 68 percent; the transactional set is 41",
            ("zero-click", "serp"),
            None,
        ),
        (
            "fact",
            "The click pattern on the money queries peaks at position 1 with 32 percent of the clicks",
            ("click-pattern", "serp"),
            None,
        ),
        (
            "fact",
            "The referral loss to the ai overviews is tracked per query with the pre and post rollout "
            "comparison",
            ("referral-loss", "ai-overview"),
            None,
        ),
        (
            "fact",
            "The gsc api pull is the daily query, page and country report with the 100000 row cap",
            ("gsc-api", "analytics"),
            None,
        ),
        (
            "fact",
            "The search data model stores the query, the url, the position, the impression and the click per "
            "day",
            ("data-model", "analytics"),
            None,
        ),
        (
            "fact",
            "The dashboard on the organic performance shows the sessions, the position and the conversion "
            "per week",
            ("dashboard", "analytics"),
            None,
        ),
        (
            "fact",
            "The reporting on the organic roi uses the conversion value against the content and the tooling "
            "cost",
            ("roi", "analytics"),
            None,
        ),
        (
            "fact",
            "The budget on the organic program is the content, the tooling and the digital pr split 50 30 20",
            ("budget", "seo"),
            None,
        ),
        (
            "fact",
            "The agency scope is the strategy and the digital pr; the content and the technical are in house",
            ("agency", "seo"),
            None,
        ),
        (
            "fact",
            "The in house team is 4: 2 content, 1 technical, 1 analyst",
            ("team", "seo"),
            None,
        ),
        (
            "fact",
            "The tooling stack is the search console, the ahrefs, the semrush and the ga4 with the data "
            "studio on top",
            ("tooling", "seo"),
            None,
        ),
        (
            "fact",
            "The licensing on the ahrefs is the advanced plan with the 50000 url tracking",
            ("licensing", "tooling"),
            None,
        ),
        (
            "fact",
            "The data pipeline for the rank tracking is the serpapi pull, the parse, the store and the alert",
            ("data-pipeline", "rank-tracking"),
            None,
        ),
        (
            "fact",
            "The alert on the rank drop fires when a money keyword falls 5 positions in a day",
            ("alert", "rank-tracking"),
            None,
        ),
        (
            "fact",
            "The notification on the index coverage drop fires when the coverage falls below 75 percent",
            ("alert", "index"),
            None,
        ),
        (
            "fact",
            "The sl on the organic program is the 95 percent uptime on the data pipeline and the 4 hour "
            "alert",
            ("slo", "seo"),
            None,
        ),
        (
            "fact",
            "The target on the money set is the top 3 position on the 20 highest-volume keywords",
            ("target", "seo"),
            None,
        ),
        (
            "fact",
            "The objective on the content program is the 40 new cluster pages over the quarter",
            ("objective", "content"),
            None,
        ),
        (
            "fact",
            "The balanced scorecard on the organic program adds the learning and the growth to the financial "
            "and the customer",
            ("balanced-scorecard", "seo"),
            None,
        ),
    ),
    (
        ("Which kind of Search Console property survives a domain move?", (0,)),
        ("bing webmaster tools verification method", (1,)),
        ("Why are the checkout and account paths disallowed in robots.txt?", (2,)),
        ("How many files make up the XML sitemap index?", (3,)),
        ("daily crawl budget in urls from the crawl stats", (4,)),
        ("What share of the site is indexed, and what is in the excluded part?", (5,)),
        ("How do faceted pages handle their canonical tag?", (6,)),
        ("hreflang languages and x-default target", (7,)),
        ("current lcp, cls and inp numbers for the landing pages", (8,)),
        ("What fixed the slow LCP on the product pages?", (9,)),
        ("Why did the article pages suffer layout shift and how was it cured?", (10,)),
        ("search results page INP typeahead debounce", (11,)),
        ("Which schema types are marked up on product pages?", (12,)),
        ("meta description length and the uplift from question phrasing", (17,)),
        ("How often do AI overviews show up for the money queries?", (24, 25)),
        ("how often the AI overview cites the site", (25,)),
        ("How often does the site hold the second slot in the local pack?", (29,)),
        ("Which rendering approach do the product pages use?", (35,)),
        ("prerendered urls refreshed hourly", (36,)),
        ("What is the split of query intent across the tracked set?", (44,)),
        ("How many keywords and locations does rank tracking cover?", (49,)),
        ("monthly organic sessions and their share of total traffic", (52,)),
        ("consent mode default for EU visitors", (60,)),
        ("How long does a GDPR deletion take to reach the backups?", (63,)),
        ("minimum detectable effect and power for landing page tests", (71,)),
        ("How large was the paid search incrementality lift against the holdout?", (73,)),
        ("Why was the manual action applied, and how did the appeal go?", (77,)),
        ("How long before a brand new site ranks stably on money keywords?", (80,)),
        ("serpapi daily volume for rank tracking", (88,)),
        ("weekly Screaming Frog crawl cap and concurrency", (91,)),
        ("What similarity threshold does near-duplicate URL clustering use?", (97, 99)),
        (
            "Why did informational referrals fall after AI overviews, and what recovered half of them?",
            (104,),
        ),
        ("What caused the LCP regression on the landing pages?", (105,)),
        ("reason index coverage dropped to 62 percent", (106,)),
        (
            "Why was server side rendering picked over client side rendering for landing pages?",
            (107,),
        ),
        ("json-ld versus microdata choice", (108,)),
        (
            "What is the zero click rate on informational queries compared with transactional ones?",
            (109,),
        ),
        ("row cap on the daily Search Console API report", (112,)),
        ("How is the organic programme budget split between content, tooling and PR?", (116,)),
        ("How many people are in the in-house team and what are their roles?", (118,)),
    ),
    (
        "How often is the robots.txt file reviewed for stale disallow rules?",
        "Which tool processes the Googlebot server logs?",
        "What is the click through rate on image search results?",
        "What share of organic sessions comes from Bing?",
        "How many pages carry video schema markup?",
        "What is the average domain rating of the main competitors?",
        "Which hosting provider serves the origin servers?",
        "How is internal site search tracked, and what share of searches return no results?",
        "What is the policy for noindexing expired job listings?",
        "How many pages exist in the AMP version of the site?",
        "What share of organic traffic comes from Google Discover?",
        "What is the average number of new backlinks acquired per month?",
    ),
)

VERSIONS: VersionDomain = VersionDomain(
    domain=DOMAIN,
    pairs=(
        VersionPair(
            *(
                (
                    "fact",
                    "Bingbot crawl-delay in robots.txt is set to 5 seconds on the product subdomain to protect the "
                    "origin servers",
                    ("bingbot", "robots"),
                    None,
                ),
                (
                    "fact",
                    "Bingbot crawl-delay in robots.txt is set to 2 seconds on the product subdomain now that the "
                    "origin sits behind the CDN",
                    ("bingbot", "robots"),
                    None,
                ),
                "How long is the Bingbot crawl-delay in robots.txt on the product subdomain?",
                "Bingbot crawl-delay in robots.txt is set to 5 seconds on the product subdomain",
                (
                    "fact",
                    "Bingbot is allowed into the image directory, while every crawler is disallowed from the staging "
                    "subdomain",
                    ("bingbot", "robots"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "XML sitemaps are regenerated every 24 hours, and lastmod only changes when the main content of "
                    "a page changes",
                    ("sitemap", "freshness"),
                    None,
                ),
                (
                    "fact",
                    "XML sitemaps are regenerated every 6 hours, and lastmod only changes when the main content of a "
                    "page changes",
                    ("sitemap", "freshness"),
                    None,
                ),
                "How often are the XML sitemaps regenerated with the lastmod values?",
                "XML sitemaps are regenerated every 24 hours",
                (
                    "fact",
                    "Sitemap files are served gzip-compressed from the CDN with a one hour cache lifetime",
                    ("sitemap", "cdn"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "The performance budget for product page LCP at the 75th percentile is 2.5 seconds on mobile "
                    "field data",
                    ("performance-budget", "lcp"),
                    None,
                ),
                (
                    "fact",
                    "The performance budget for product page LCP at the 75th percentile is 2.0 seconds on mobile "
                    "field data, tightened after the image work",
                    ("performance-budget", "lcp"),
                    None,
                ),
                "What is the LCP performance budget for product pages on mobile?",
                "The performance budget for product page LCP at the 75th percentile is 2.5 seconds on mobile "
                "field data",
                (
                    "fact",
                    "The performance budget for JavaScript on product pages is 300 KB compressed",
                    ("performance-budget", "javascript"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "Rank tracker keeps raw SERP snapshots for 90 days before rolling them up into weekly median "
                    "positions",
                    ("rank-tracking", "retention"),
                    None,
                ),
                (
                    "fact",
                    "Rank tracker keeps raw SERP snapshots for 180 days before rolling them up into weekly median "
                    "positions, so update analysis can reach further back",
                    ("rank-tracking", "retention"),
                    None,
                ),
                "How long does the rank tracker keep raw SERP snapshots before rolling them up?",
                "Rank tracker keeps raw SERP snapshots for 90 days before rolling them up into weekly median "
                "positions",
                (
                    "fact",
                    "Rank tracker snapshots are stored as compressed JSON in the cold bucket, with the weekly "
                    "medians in a separate table",
                    ("rank-tracking", "storage"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "The canonical mismatch alert fires when more than 3 percent of sampled URLs have a "
                    "Google-selected canonical that differs from the declared one",
                    ("canonical", "alert"),
                    None,
                ),
                (
                    "fact",
                    "The canonical mismatch alert fires when more than 5 percent of sampled URLs have a "
                    "Google-selected canonical that differs from the declared one",
                    ("canonical", "alert"),
                    None,
                ),
                "At what share of sampled URLs does the canonical mismatch alert fire?",
                "The canonical mismatch alert fires when more than 3 percent of sampled URLs have a "
                "Google-selected canonical that differs from the declared one",
                (
                    "fact",
                    "Each canonical audit run samples 2000 URLs through the URL Inspection API, stratified by page "
                    "template",
                    ("canonical", "audit"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "Internal redirect chains longer than 2 hops are logged as defects in the weekly crawl and fixed "
                    "in the next sprint",
                    ("redirects", "crawl"),
                    None,
                ),
                (
                    "fact",
                    "Internal redirect chains longer than 1 hop are logged as defects in the weekly crawl and fixed "
                    "in the next sprint",
                    ("redirects", "crawl"),
                    None,
                ),
                "How many hops make an internal redirect chain a defect in the weekly crawl?",
                "Internal redirect chains longer than 2 hops are logged as defects in the weekly crawl",
                (
                    "fact",
                    "Redirects for retired product URLs use a 301 to the closest category page and stay in place for "
                    "12 months",
                    ("redirects", "migration"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "The CMS template caps image alt text at 125 characters, and the field is required for every "
                    "editorial image",
                    ("images", "cms"),
                    None,
                ),
                (
                    "fact",
                    "The CMS template caps image alt text at 100 characters, and the field is required for every "
                    "editorial image",
                    ("images", "cms"),
                    None,
                ),
                "What is the maximum length of image alt text in the CMS template?",
                "The CMS template caps image alt text at 125 characters",
                (
                    "fact",
                    "Image file names use lowercase hyphenated product slugs, and the CMS rejects uploads with "
                    "spaces in the name",
                    ("images", "cms"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "The nightly index audit uses 1000 URL Inspection API requests per property, leaving the rest of "
                    "the 2000 daily quota for ad hoc checks",
                    ("url-inspection", "index"),
                    None,
                ),
                (
                    "fact",
                    "The nightly index audit uses 1500 URL Inspection API requests per property, leaving the rest of "
                    "the 2000 daily quota for ad hoc checks",
                    ("url-inspection", "index"),
                    None,
                ),
                "How many URL Inspection API requests does the nightly index audit use per property?",
                "The nightly index audit uses 1000 URL Inspection API requests per property",
                (
                    "fact",
                    "URL Inspection API calls are made through a service account with restricted user access to the "
                    "property",
                    ("url-inspection", "access"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "Articles that lose more than 20 percent of their clicks over 90 days are queued for a refresh",
                    ("content-decay", "content"),
                    None,
                ),
                (
                    "fact",
                    "Articles that lose more than 30 percent of their clicks over 90 days are queued for a refresh, "
                    "to keep the editors' backlog manageable",
                    ("content-decay", "content"),
                    None,
                ),
                "Which click loss over 90 days queues an article for a refresh?",
                "Articles that lose more than 20 percent of their clicks over 90 days are queued for a refresh",
                (
                    "fact",
                    "Refreshed articles keep their original URL and get a visible updated date in the byline",
                    ("content-decay", "content"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "Orphan pages found in the crawl are given at least 3 internal links from related cluster pages "
                    "before the next sitemap build",
                    ("orphan-pages", "internal-links"),
                    None,
                ),
                (
                    "fact",
                    "Orphan pages found in the crawl are given at least 5 internal links from related cluster pages "
                    "before the next sitemap build",
                    ("orphan-pages", "internal-links"),
                    None,
                ),
                "How many internal links does an orphan page get before the next sitemap build?",
                "Orphan pages found in the crawl are given at least 3 internal links from related cluster pages",
                (
                    "fact",
                    "Pages with no organic clicks for a year are reviewed for merging or removal during the annual "
                    "content audit",
                    ("orphan-pages", "content-audit"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "Category page titles follow the pattern Category | Brand, and the CMS truncates them at 580 "
                    "pixels",
                    ("title", "templates"),
                    None,
                ),
                (
                    "fact",
                    "Category page titles follow the pattern Category | Brand, and the CMS truncates them at 600 "
                    "pixels",
                    ("title", "templates"),
                    None,
                ),
                "At what pixel width does the CMS truncate category page titles?",
                "Category page titles follow the pattern Category | Brand, and the CMS truncates them at 580 "
                "pixels",
                (
                    "fact",
                    "Product page titles include a colour or size only when that variant has its own indexed URL",
                    ("title", "templates"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "The warehouse keeps 60 days of verified Googlebot requests from the server logs for crawl "
                    "analysis",
                    ("log-analysis", "retention"),
                    None,
                ),
                (
                    "fact",
                    "The warehouse keeps 90 days of verified Googlebot requests from the server logs for crawl "
                    "analysis",
                    ("log-analysis", "retention"),
                    None,
                ),
                "How many days of Googlebot requests from the server logs does the warehouse keep?",
                "The warehouse keeps 60 days of verified Googlebot requests from the server logs",
                (
                    "fact",
                    "Googlebot requests are verified by reverse DNS lookup before they are counted in log analysis",
                    ("log-analysis", "verification"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "GA4 event data retention on the web property is set to 2 months, the shortest option offered",
                    ("ga4", "retention"),
                    None,
                ),
                (
                    "fact",
                    "GA4 event data retention on the web property is set to 14 months, the longest option offered, "
                    "so year-on-year explorations work",
                    ("ga4", "retention"),
                    None,
                ),
                "What event data retention is configured on the GA4 web property?",
                "GA4 event data retention on the web property is set to 2 months",
                (
                    "fact",
                    "The GA4 internal traffic filter excludes the office IP ranges and is in the active state",
                    ("ga4", "filters"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "Partition expiry on the Search Console bulk export tables in BigQuery is 400 days",
                    ("search-console", "bigquery"),
                    None,
                ),
                (
                    "fact",
                    "Partition expiry on the Search Console bulk export tables in BigQuery is 700 days, which lets "
                    "reports compare two full years",
                    ("search-console", "bigquery"),
                    None,
                ),
                "What is the partition expiry on the Search Console bulk export tables in BigQuery?",
                "Partition expiry on the Search Console bulk export tables in BigQuery is 400 days",
                (
                    "fact",
                    "The Search Console bulk export dataset lives in the EU multi-region location",
                    ("search-console", "bigquery"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "Digital PR outreach is capped at 40 journalist pitches per campaign week",
                    ("digital-pr", "outreach"),
                    None,
                ),
                (
                    "fact",
                    "Digital PR outreach is capped at 25 journalist pitches per campaign week, after reply rates "
                    "fell on the larger sends",
                    ("digital-pr", "outreach"),
                    None,
                ),
                "How many journalist pitches per campaign week does digital PR outreach allow?",
                "Digital PR outreach is capped at 40 journalist pitches per campaign week",
                (
                    "fact",
                    "Digital PR pitches go out between Tuesday and Thursday mornings, UK time",
                    ("digital-pr", "outreach"),
                    None,
                ),
            )
        ),
        VersionPair(
            *(
                (
                    "fact",
                    "More than 10 pages with structured data errors in the Search Console enhancement report pages "
                    "the on-call SEO",
                    ("structured-data", "alert"),
                    None,
                ),
                (
                    "fact",
                    "More than 25 pages with structured data errors in the Search Console enhancement report pages "
                    "the on-call SEO",
                    ("structured-data", "alert"),
                    None,
                ),
                "How many pages with structured data errors page the on-call SEO?",
                "More than 10 pages with structured data errors in the Search Console enhancement report pages "
                "the on-call SEO",
                (
                    "fact",
                    "Structured data warnings are reviewed in the monthly template audit and do not page anyone",
                    ("structured-data", "audit"),
                    None,
                ),
            )
        ),
    ),
)

ANSWERABLE: tuple[tuple[str, tuple[int, ...]], ...] = DOMAIN.answerable

UNANSWERED: tuple[str, ...] = DOMAIN.unanswered
