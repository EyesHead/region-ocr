"""Measure connected source-rule segments without inventing missing borders."""


def joined_vertical_rule_end(edges, axis, box, *, tolerance=2):
    """Return the proven end above a note, following contiguous PDF segments.

    Browser PDFs often paint a long border as one rectangle per displayed
    record. The end of its first rectangle is not the end of the border.
    A real gap terminates the chain; a full-height rule cannot prove a note.
    """
    pieces = sorted((edge['top'], edge['bottom']) for edge in edges
                    if edge.get('orientation') == 'v' and abs(edge['x0'] - axis) < tolerance
                    and edge['bottom'] > box[1] - 3 and edge['top'] < box[3])
    end = None
    for top, bottom in pieces:
        if end is None:
            if top <= box[1] + 3 and bottom >= box[1] - 3:
                end = bottom
        elif top <= end + tolerance:
            end = max(end, bottom)
        else:
            break
    return end if end is not None and box[1] + 8 < end < box[3] - 5 else None
