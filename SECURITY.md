# Security

This package runs inside other people's applications and sends their images to an external service.
It treats every input as hostile: the image, its name, the path it is read from, the configuration
passed in and the services' answers.

## What is protected

### 1. Only genuine images are sent

- **Content decides, not the name.** The format is identified from the magic bytes; the file extension
  and the MIME type a browser reported are ignored. Accepted: JPEG, PNG, GIF, WebP, AVIF, HEIC. SVG is
  never accepted: it is a document that can carry scripts.
- **One decoder per file.** Pillow opens the file with the plugin of the identified format only, so a file
  cannot be steered into another decoder.
- **Decompression bombs.** Width, height and the pixel count are read from the header and checked against
  the policy (20000 px a side, 50 million pixels by default) before a single pixel is decoded. Pillow's
  own bomb check stays in place above that.
- **Size.** Files above the policy limit (at most the API's 10 MiB) are refused before they are read whole;
  a file on disk is read with a hard cap even if it grows while it is read.
- **Broken files.** Anything Pillow cannot decode completely - a truncated JPEG, a PNG with a wrong
  checksum, HTML behind a GIF signature - is refused. Pillow's message, which may quote the file, is not
  passed on.

### 2. Nothing besides the pixels leaves

The image sent is always a new one: decoded, turned upright by its EXIF orientation, scaled down to the
policy's longest side (2048 px), copied into a fresh image from its raw pixels and encoded as JPEG
(opaque) or WebP (transparent). EXIF and GPS, XMP, IPTC, ICC profiles, comments, text chunks, embedded
thumbnails, further frames and anything after the end of the file cannot travel with it. Only
encoder-written bytes leave the server; the output is opened again before it is sent.

### 3. Files outside the allowed directories are not read

`ImageSource.from_path()` exists so that a path from a request (`../../settings.py`) cannot turn the
generator into a file-reading primitive.

- A directory must be named; it is resolved with `realpath()`.
- The file's real path, symlinks resolved, must lie inside it (`/srv/media-old` is not inside
  `/srv/media`). URLs (`file://`, `http://`, `data:`) and NUL bytes are refused.
- The file is opened without following a final symlink, must be a regular file (not a FIFO or a device),
  and its inode must match the checked path, so a swap between the check and the read is detected.
- Strings and `Path` objects passed to `generate()` are refused: reading a path is always explicit.

### 4. The account key does not leak

- Keys, the connection secret and the proxy URL are held in `Secret`, which shows as `[redacted]` in
  `str()`, `repr()`, f-strings and debuggers' variable views, and refuses to be pickled, so it cannot slip
  into a Celery message, a cache or a session. `Config`'s `repr()` hides them too.
- The API takes the key as a `key` query parameter. It is added by an `httpx` auth hook at the last moment,
  so the request objects this package holds do not carry it, and it is masked in the `httpx` logger's
  `HTTP Request: ...` lines (a filter on the `httpx` logger replaces the value with `[redacted]`).
- Every error message passes through a redactor that removes the key and the secret, raw and URL-encoded,
  and strips the query of any URL. The services' messages are also cleaned to plain text. Errors are
  raised `from None`, so the `httpx` exception (which holds the request) is not printed with them.
- The key is never placed in the uploaded file, its name or a header.

The query travels inside TLS, so it is not visible on the network, but it can end up in the access logs
of the API's own reverse proxy.

What this package cannot hide: an error tracker that records **local variables** of every frame (Sentry's
`include_local_variables`, the default there) can see request bodies in the frames of the connection calls,
which carry the connection secret, and of `connect()`, which carries the password. Scrub `secret`,
`password` and `key` in such a tool, or switch local variables off for these frames.

### 5. The connection

- HTTPS only, with certificate and host name verification against certifi's bundle (or `SSL_CERT_FILE`,
  or a configured CA bundle), TLS 1.2 or newer. A failed handshake is an error, never a fallback to plain
  HTTP. Plain HTTP is possible only for 127.0.0.1, ::1 and localhost with `allow_insecure_loopback`, for tests.
- Redirects are never followed: the key and the image go only to the configured host.
- Connect and total timeouts are always set; an answer above 1 MiB (after decompression) aborts the transfer.
- Only failures before anything was sent (DNS, a refused connection, a connect timeout) are retried, so an
  image is never submitted twice and the limit is not used twice. Connect and disconnect are never repeated:
  a repeated failed login counts towards the account's lockout.

### 6. Answers from the services

- JSON is decoded with Python's limits; identifiers must be UUIDs; alts must be non-empty strings.
- Alts are cleaned to one line of plain text: invalid UTF-8, markup, control characters, bidirectional
  overrides and isolates ("Trojan Source"), zero-width characters and BOMs are removed, and the length is
  capped. This is not HTML escaping: templates still escape the alt where it is printed.

### 7. Pushes from dockaccess.org

`parse_push()` and the framework receivers verify the HMAC-SHA256 signature over the timestamp and the exact
body with the connection secret before reading anything, compare in constant time, accept five minutes of
clock skew, cap the body at 64 KiB, answer every signature problem with the same 401, refuse a site token
that is not this site's (403), and clean the alts like generated ones.

## What this package does not do

- It does not protect your application from the uploaded file itself. Whether your application stores it,
  and how it serves it, is your application's concern.
- It is not a malware scanner. It refuses what is not a well-formed image; it does not judge what an image
  shows.
- It does not store anything: no temporary files, no cache, no logs - apart from the `.env` file and the
  manifest the CLI writes when told to.

## Reporting a vulnerability

Please report vulnerabilities privately to Dock sp. z o.o. (https://dock.eu) rather than in a public issue.
Include the package version, the Python version and, if possible, a file or a script that reproduces the problem.
