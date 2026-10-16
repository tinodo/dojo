"""Source discovery v2 (ADR 0018): finding official pages that teach every skill of an added exam.

Models propose search strings; search engines and the documentation's own tables of contents supply candidate
pages; Dojo fetches every page and two model families judge it; a page counts only when both agree and both
quote a sentence that is on the page. Dojo, not a model, keeps the books (state, ledger, lease).
"""
