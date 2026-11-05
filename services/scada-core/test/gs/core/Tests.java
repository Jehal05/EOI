package gs.core;

public final class Tests {
    public static void main(String[] a) throws Exception {
        FoundationTests.run();
        ClusterTests.run();
        AsymTokenTests.run();
        BusTests.run();
        FuzzTests.run();
        try { Class.forName("gs.core.EngineTests").getMethod("run").invoke(null); } catch (ClassNotFoundException ignore) { }
        System.out.println("passed=" + T.pass + " failed=" + T.fail);
        System.exit(T.fail == 0 ? 0 : 1);
    }
}
