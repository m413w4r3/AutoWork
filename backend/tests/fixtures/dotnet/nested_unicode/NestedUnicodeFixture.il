// IL code: NestedUnicodeFixture
.class private auto ansi '<Module>'
{
} // end of class <Module>

.class public auto ansi abstract sealed beforefieldinit BenignFixtures.Outer
	extends [System.Runtime]System.Object
{
	// Nested Types
	.class nested public auto ansi abstract sealed beforefieldinit Inner
		extends [System.Runtime]System.Object
	{
		// Methods
		.method public hidebysig static 
			int32 Answer () cil managed 
		{
			// Method begins at RVA 0x2054
			// Header size: 1
			// Code size: 3 (0x3)
			.maxstack 8

			IL_0000: ldc.i4.s 42
			IL_0002: ret
		} // end of method Inner::Answer

		.method public hidebysig static 
			int32 Answer (
				int32 'value'
			) cil managed 
		{
			// Method begins at RVA 0x2058
			// Header size: 1
			// Code size: 4 (0x4)
			.maxstack 8

			IL_0000: ldarg.0
			IL_0001: ldc.i4.1
			IL_0002: add
			IL_0003: ret
		} // end of method Inner::Answer

		.method public hidebysig static 
			int32 대답 (
				int32 'value'
			) cil managed 
		{
			// Method begins at RVA 0x205d
			// Header size: 1
			// Code size: 4 (0x4)
			.maxstack 8

			IL_0000: ldarg.0
			IL_0001: ldc.i4.1
			IL_0002: add
			IL_0003: ret
		} // end of method Inner::대답

	} // end of class Inner


	// Methods
	.method public hidebysig static 
		int32 OuterAnswer () cil managed 
	{
		// Method begins at RVA 0x2050
		// Header size: 1
		// Code size: 3 (0x3)
		.maxstack 8

		IL_0000: ldc.i4.s 42
		IL_0002: ret
	} // end of method Outer::OuterAnswer

} // end of class BenignFixtures.Outer

