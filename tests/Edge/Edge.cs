namespace Edge.Cases
{
    /// <summary>Documents the whole class.</summary>
    public class Widget
    {
        private int _count;

        /// <summary>
        /// Builds a widget.
        /// </summary>
        /// <param name="count">how many</param>
        public Widget(
            int count,
            string name)
        {
            _count = count;
        }

        // ordinary line comment, not XML-doc — still a preamble of the method below
        public int Increment()
        {
            if (_count > 0)
            {
                _count++;
            }

            return _count;
        }

        /* block comment
           spanning lines,
           preamble of Reset */
        public void Reset()
        {
            _count = 0;
        }

        public void Trailing()
        {
            var x = _count;
            // trailing comment: documents nothing below, must NOT glue to a block
        }

        /// <summary>A nested type inside Widget.</summary>
        public class Inner
        {
            public void Deep()
            {
                for (int i = 0; i < 3; i++)
                {
                    System.Console.WriteLine(i);
                }
            }
        }
    }
}
