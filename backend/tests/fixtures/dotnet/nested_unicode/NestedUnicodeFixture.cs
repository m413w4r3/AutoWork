namespace BenignFixtures;

public static class Outer
{
    public static int OuterAnswer() => 42;

    public static class Inner
    {
        public static int Answer() => 42;
        public static int Answer(int value) => value + 1;

        public static int 대답(int value) => value + 1;
    }
}
