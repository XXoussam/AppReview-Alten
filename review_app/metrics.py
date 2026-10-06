import re


def normalize(text):
  text = re.sub(r"[^\w\s']", ' ', (text or '').lower())
  return text.split()


def word_errors(reference, hypothesis):
  """Levenshtein distance over words: (errors, reference_word_count)."""
  ref, hyp = normalize(reference), normalize(hypothesis)
  prev = list(range(len(hyp) + 1))
  for i, r in enumerate(ref, 1):
    cur = [i] + [0] * len(hyp)
    for j, h in enumerate(hyp, 1):
      cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
    prev = cur
  return prev[-1], len(ref)


def corpus_wer(pairs):
  """pairs = [(human_text, model_text), ...]; returns WER over all words, or None."""
  errors = words = 0
  for ref, hyp in pairs:
    e, n = word_errors(ref, hyp)
    errors += e
    words += n
  return round(errors / words, 4) if words else None
