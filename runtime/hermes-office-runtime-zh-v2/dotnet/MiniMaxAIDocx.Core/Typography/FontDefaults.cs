namespace MiniMaxAIDocx.Core.Typography;

public record FontConfig(
    string BodyFont,
    string HeadingFont,
    double BodySize,
    double Heading1Size,
    double Heading2Size,
    double Heading3Size,
    double Heading4Size,
    double Heading5Size,
    double Heading6Size,
    double LineSpacing);

/// <summary>
/// Default font configurations by document type.
/// </summary>
public static class FontDefaults
{
    public static FontConfig Report => new("Noto Sans CJK SC", "Noto Serif CJK SC", 11.0, 26.0, 20.0, 16.0, 14.0, 12.0, 11.0, 1.15);
    public static FontConfig Letter => new("Noto Sans CJK SC", "Noto Sans CJK SC", 11.0, 16.0, 14.0, 12.0, 11.0, 11.0, 11.0, 1.0);
    public static FontConfig Memo => new("Noto Sans CJK SC", "Noto Sans CJK SC", 11.0, 16.0, 14.0, 12.0, 11.0, 11.0, 11.0, 1.15);
    public static FontConfig Academic => new("Noto Serif CJK SC", "Noto Serif CJK SC", 12.0, 16.0, 14.0, 13.0, 12.0, 12.0, 12.0, 2.0);
}
