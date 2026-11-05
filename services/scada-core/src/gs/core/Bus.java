package gs.core;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicLong;

/** Publisher to a Redis Streams message bus (first-party RESP client; XADD only).
 *  Best-effort by design: the control core never waits on the bus. A failed publish is counted and dropped, the connection
 *  is retried with a short back-off. Consumers get at-least-once delivery from the stream and must tolerate repeats
 *  (the historian's writes are idempotent). No TLS in this client: use it on a protected network segment or a tunnel. */
public final class Bus {
    private final String host; private final int port; private final String password; private final String stream; private final long maxLen;
    private Socket sock; private OutputStream out; private InputStream in; private long retryAt;
    public final AtomicLong published = new AtomicLong(), dropped = new AtomicLong();

    public Bus(String url, String stream, long maxLen) {
        URI u = URI.create(url);
        if (!"redis".equals(u.getScheme()) || u.getHost() == null) throw new IllegalArgumentException("bus URL must be redis://[:password@]host[:port]");
        this.host = u.getHost(); this.port = u.getPort() > 0 ? u.getPort() : 6379;
        String ui = u.getUserInfo(); this.password = ui == null ? null : ui.substring(ui.indexOf(':') + 1);
        this.stream = stream; this.maxLen = maxLen;
    }

    private void connect() throws IOException {
        sock = new Socket(); sock.connect(new InetSocketAddress(host, port), 1000); sock.setSoTimeout(1500); sock.setTcpNoDelay(true);
        out = new BufferedOutputStream(sock.getOutputStream()); in = new BufferedInputStream(sock.getInputStream());
        if (password != null && !password.isEmpty()) { send("AUTH", password); reply(); }
    }

    private void send(String... parts) throws IOException {
        out.write(("*" + parts.length + "\r\n").getBytes(StandardCharsets.US_ASCII));
        for (String p : parts) { byte[] b = p.getBytes(StandardCharsets.UTF_8); out.write(("$" + b.length + "\r\n").getBytes(StandardCharsets.US_ASCII)); out.write(b); out.write("\r\n".getBytes(StandardCharsets.US_ASCII)); }
        out.flush();
    }

    private String reply() throws IOException {
        StringBuilder sb = new StringBuilder(); int c;
        while ((c = in.read()) != '\n') { if (c < 0) throw new EOFException(); if (c != '\r') sb.append((char) c); if (sb.length() > 512) throw new IOException("reply too long"); }
        String l = sb.toString();
        if (l.startsWith("-")) throw new IOException("bus error: " + l);
        if (l.startsWith("$")) { int n = Integer.parseInt(l.substring(1)); if (n >= 0) in.readNBytes(n + 2); }
        return l;
    }

    /** Publish one message; returns false (and counts a drop) when the bus is unreachable. Synchronised: one publisher thread. */
    public synchronized boolean publish(String json) {
        long now = System.currentTimeMillis();
        try {
            if (sock == null) { if (now < retryAt) { dropped.incrementAndGet(); return false; } connect(); }
            send("XADD", stream, "MAXLEN", "~", Long.toString(maxLen), "*", "d", json);
            reply();
            published.incrementAndGet();
            return true;
        } catch (Exception e) {
            try { if (sock != null) sock.close(); } catch (IOException ignore) { }
            sock = null; retryAt = now + 2000; dropped.incrementAndGet();
            return false;
        }
    }
}
