# Resumes

Drop PDF / DOCX / MD resume files here. The CLI auto-loads them when `config.resumes` is empty.

Optional explicit entries in `config.yaml`:

```yaml
resumes:
  - file: backend.pdf
    label: backend
    include: [Django, PostgreSQL, AWS, Redis]
```

`include` boosts ranking for that resume’s cover letter; global `must_have` still drives portal search.
