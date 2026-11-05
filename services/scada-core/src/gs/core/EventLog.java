package gs.core;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.function.Consumer;

/**
 * Append-only, HMAC-chained event log. Line = JSON \t HMAC(key, prevHmac + json).
 * Any edit, deletion or reordering breaks the chain; without the key the chain cannot be recomputed.
 */
public final class EventLog implements Closeable {
    public record Result(long records, long lastSeq, String lastHash, long truncatedBytes) {}

    private final Path path;
    private final byte[] key;
    private FileOutputStream fos;
    private BufferedOutputStream out;
    private String prev = "GENESIS";

    public EventLog(Path path, byte[] key) { this.path = path; this.key = key; }

    /** Verify the whole chain, feed each record to `sink`, repair a torn final line, then open for append. */
    public Result open(Consumer<Rec> sink) throws IOException {
        Result r = read(path, key, sink, true);
        prev = r.lastHash();
        fos = new FileOutputStream(path.toFile(), true);
        out = new BufferedOutputStream(fos, 1 << 16);
        return r;
    }

    public static Result read(Path path, byte[] key, Consumer<Rec> sink, boolean repair) throws IOException {
        if (!Files.exists(path)) return new Result(0, 0, "GENESIS", 0);
        String prev = "GENESIS";
        long n = 0, last = 0, good = 0, pos = 0;
        long size = Files.size(path);
        try (InputStream in = new BufferedInputStream(Files.newInputStream(path), 1 << 16)) {
            ByteArrayOutputStream line = new ByteArrayOutputStream(256);
            int c;
            while ((c = in.read()) >= 0) {
                pos++;
                if (c != '\n') { line.write(c); continue; }
                String s = line.toString(StandardCharsets.UTF_8);
                line.reset();
                int tab = s.lastIndexOf('\t');
                if (tab < 0) throw new IOException("event log corrupt at record " + (n + 1) + ": no chain tag");
                String json = s.substring(0, tab), h = s.substring(tab + 1);
                if (!Token.hmacHex(key, prev + json).equals(h)) throw new IOException("event log chain broken at record " + (n + 1) + " (tampering or wrong key)");
                Rec r = Rec.fromMap(Json.map(Json.parse(json)));
                sink.accept(r);
                prev = h; n++; last = r.seq; good = pos;
            }
            if (line.size() > 0) {                          // torn final line from a crash
                if (!repair) throw new IOException("event log ends with a partial record");
            }
        }
        long cut = size - good;
        if (cut > 0 && repair) try (FileChannel2 fc = new FileChannel2(path)) { fc.truncate(good); }
        return new Result(n, last, prev, cut);
    }

    public synchronized void append(Rec r) throws IOException {
        String json = Json.str(r.toMap());
        String h = Token.hmacHex(key, prev + json);
        out.write(json.getBytes(StandardCharsets.UTF_8));
        out.write('\t');
        out.write(h.getBytes(StandardCharsets.US_ASCII));
        out.write('\n');
        prev = h;
    }

    public synchronized void flush() throws IOException { out.flush(); }
    public synchronized void sync() throws IOException { out.flush(); fos.getFD().sync(); }
    public synchronized String head() { return prev; }
    @Override public synchronized void close() throws IOException { if (out != null) { sync(); out.close(); } }

    /** Small helper so truncate works through the channel API. */
    private static final class FileChannel2 implements Closeable {
        private final java.nio.channels.FileChannel ch;
        FileChannel2(Path p) throws IOException { ch = java.nio.channels.FileChannel.open(p, StandardOpenOption.WRITE); }
        void truncate(long n) throws IOException { ch.truncate(n); }
        @Override public void close() throws IOException { ch.close(); }
    }
}
