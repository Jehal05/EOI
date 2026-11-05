package gs.core;

/** Tiny assertion helper (no test framework is available offline). */
final class T {
    static int pass, fail;
    static void eq(String name, Object want, Object got) {
        if (java.util.Objects.equals(want, got)) pass++;
        else { fail++; System.out.println("FAIL " + name + ": want <" + want + "> got <" + got + ">"); }
    }
    static void ok(String name, boolean c) { eq(name, true, c); }
    static void throwsA(String name, Runnable r) {
        try { r.run(); fail++; System.out.println("FAIL " + name + ": expected exception"); } catch (RuntimeException e) { pass++; }
    }
}
