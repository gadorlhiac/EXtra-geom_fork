from cfelpyutils.crystfel_utils import load_crystfel_geometry
from copy import copy
import numpy as np
from scipy.ndimage import affine_transform
import warnings

def _crystfel_format_vec(vec):
    """Convert an array of 3 numbers to CrystFEL format like "+1.0x -0.1y"
    """
    s = '{:+}x {:+}y'.format(*vec[:2])
    if vec[2] != 0:
        s += ' {:+}z'.format(vec[2])
    return s

class GeometryFragment:

    # The coordinates in this class are (x, y, z), in pixel units
    def __init__(self, corner_pos, ss_vec, fs_vec, ss_pixels, fs_pixels):
        self.corner_pos = corner_pos
        self.ss_vec = ss_vec
        self.fs_vec = fs_vec
        self.ss_pixels = ss_pixels
        self.fs_pixels = fs_pixels

    @classmethod
    def from_panel_dict(cls, d):
        corner_pos = np.array([d['cnx'], d['cny'], d['coffset']])
        ss_vec = np.array([d['ssx'], d['ssy'], d['ssz']])
        fs_vec = np.array([d['fsx'], d['fsy'], d['fsz']])
        ss_pixels = d['max_ss'] - d['min_ss'] + 1
        fs_pixels = d['max_fs'] - d['min_fs'] + 1
        return cls(corner_pos, ss_vec, fs_vec, ss_pixels, fs_pixels)

    def corners(self):
        return np.stack([
            self.corner_pos,
            self.corner_pos + (self.fs_vec * self.fs_pixels),
            self.corner_pos + (self.ss_vec * self.ss_pixels) + (self.fs_vec * self.fs_pixels),
            self.corner_pos + (self.ss_vec * self.ss_pixels),
        ])

    def centre(self):
        return self.corner_pos + (.5 * self.ss_vec * self.ss_pixels) \
                               + (.5 * self.fs_vec * self.fs_pixels)

    def to_crystfel_geom(self, p, a):
        name = 'p{}a{}'.format(p, a)
        c = self.corner_pos
        return CRYSTFEL_PANEL_TEMPLATE.format(
            name=name, p=p,
            min_ss=(a * self.ss_pixels), max_ss=(((a + 1) * self.ss_pixels) - 1),
            ss_vec=_crystfel_format_vec(self.ss_vec),
            fs_vec=_crystfel_format_vec(self.fs_vec),
            corner_x=c[0], corner_y=c[1], coffset=c[2],
        )

    def snap(self):
        corner_pos = np.around(self.corner_pos[:2]).astype(np.int32)
        ss_vec = np.around(self.ss_vec[:2]).astype(np.int32)
        fs_vec = np.around(self.fs_vec[:2]).astype(np.int32)
        assert {tuple(np.abs(ss_vec)), tuple(np.abs(fs_vec))} == {(0, 1), (1, 0)}
        # Convert xy coordinates to yx indexes
        return GridGeometryFragment(corner_pos[::-1], ss_vec[::-1], fs_vec[::-1],
                                    self.ss_pixels, self.fs_pixels)


class GridGeometryFragment:
    # These coordinates are all (y, x), suitable for indexing a numpy array.
    def __init__(self, corner_pos, ss_vec, fs_vec, ss_pixels, fs_pixels):
        self.ss_vec = ss_vec
        self.fs_vec = fs_vec
        self.ss_pixels = ss_pixels
        self.fs_pixels = fs_pixels

        if fs_vec[0] == 0:
            # Flip without transposing
            fs_order = fs_vec[1]
            ss_order = ss_vec[0]
            self.transform = lambda arr: arr[..., ::ss_order, ::fs_order]
            corner_shift = np.array([
                min(ss_order, 0) * self.ss_pixels,
                min(fs_order, 0) * self.fs_pixels
            ])
            self.pixel_dims = np.array([self.ss_pixels, self.fs_pixels])
        else:
            # Transpose and then flip
            fs_order = fs_vec[0]
            ss_order = ss_vec[1]
            self.transform = lambda arr: arr.swapaxes(-1, -2)[..., ::fs_order, ::ss_order]
            corner_shift = np.array([
                min(fs_order, 0) * self.fs_pixels,
                min(ss_order, 0) * self.ss_pixels
            ])
            self.pixel_dims = np.array([self.fs_pixels, self.ss_pixels])
        self.corner_idx = corner_pos + corner_shift
        self.opp_corner_idx = self.corner_idx + self.pixel_dims


class DetectorGeometryBase:
    """Base class for detector geometry. Subclassed for specific detectors."""
    # Define in subclasses:
    pixel_size = 0.
    frag_ss_pixels = 0
    frag_fs_pixels = 0
    expected_data_shape = ()

    def __init__(self, modules, filename='No file'):
        self.modules = modules  # List of 16 lists of 8 fragments
        # self.filename is metadata for plots, we don't read/write the file.
        # There are separate methods for reading and writing.
        self.filename = filename
        self._snapped_cache = None

    def _snapped(self):
        """Snap geometry to a 2D pixel grid

        This returns a new geometry object. The 'snapped' geometry is
        less accurate, but can assemble data into a 2D array more efficiently,
        because it doesn't do any interpolation.
        """
        if self._snapped_cache is None:
            new_modules = []
            for module in self.modules:
                new_tiles = [t.snap() for t in module]
                new_modules.append(new_tiles)
            self._snapped_cache = SnappedGeometry(new_modules, self)
        return self._snapped_cache

    @staticmethod
    def split_tiles(module_data):
        """Split data from a detector module into tiles.

        Must be implemented in subclasses.
        """
        raise NotImplementedError

    def position_modules_fast(self, data):
        """Assemble data from this detector according to where the pixels are.

        This approximates the geometry to align all pixels to a 2D grid. It's
        less accurate than :meth:`position_modules_interpolate`, but much faster.

        Parameters
        ----------

        data : ndarray
          The last three dimensions should be channelno, pixel_ss, pixel_fs
          (lengths 16, 512, 128). ss/fs are slow-scan and fast-scan.

        Returns
        -------
        out : ndarray
          Array with one dimension fewer than the input.
          The last two dimensions represent pixel y and x in the detector space.
        centre : ndarray
          (y, x) pixel location of the detector centre in this geometry.
        """
        return self._snapped().position_modules(data)

    def position_all_modules(self, data):
        """Deprecated alias for :meth:`position_modules_fast`"""
        return self.position_modules_fast(data)

    def plot_data_fast(self, data, axis_units='px'):
        """Plot data from the detector using this geometry.

        This approximates the geometry to align all pixels to a 2D grid.

        Returns a matplotlib figure.

        Parameters
        ----------

        data : ndarray
          Should have exactly 3 dimensions: channelno, pixel_ss, pixel_fs
          (lengths 16, 512, 128). ss/fs are slow-scan and fast-scan.
        axis_units : str
          Show the detector scale in pixels ('px') or metres ('m').
        """
        return self._snapped().plot_data(data, axis_units=axis_units)

class AGIPD_1MGeometry(DetectorGeometryBase):
    """Detector layout for AGIPD-1M

    The coordinates used in this class are 3D (x, y, z), and represent multiples
    of the pixel size.
    """
    pixel_size = 2e-4  # 2e-4 metres == 0.2 mm
    frag_ss_pixels = 64
    frag_fs_pixels = 128
    expected_data_shape = (16, 512, 128)

    @classmethod
    def from_quad_positions(cls, quad_pos, asic_gap=2, panel_gap=29):
        """Generate an AGIPD-1M geometry from quadrant positions.

        This produces an idealised geometry, assuming all modules are perfectly
        flat, aligned and equally spaced within their quadrant.

        The quadrant positions are given in pixel units, referring to the first
        pixel of the first module in each quadrant, corresponding to data
        channels 0, 4, 8 and 12.
        """
        quads_x_orientation = [1, 1, -1, -1]
        quads_y_orientation = [-1, -1, 1, 1]
        modules = []
        for p in range(16):
            quad = p // 4
            quad_corner = quad_pos[quad]
            x_orient = quads_x_orientation[quad]
            y_orient = quads_y_orientation[quad]
            p_in_quad = p % 4
            corner_y = quad_corner[1] - (p_in_quad * (128 + panel_gap))

            tiles = []
            modules.append(tiles)

            for a in range(8):
                corner_x = quad_corner[0] + x_orient * (64 + asic_gap) * a
                tiles.append(GeometryFragment(
                    corner_pos=np.array([corner_x, corner_y, 0.]),
                    ss_vec=np.array([x_orient, 0, 0]),
                    fs_vec=np.array([0, y_orient, 0]),
                    ss_pixels=cls.frag_ss_pixels,
                    fs_pixels=cls.frag_fs_pixels,
                ))
        return cls(modules)

    @classmethod
    def from_crystfel_geom(cls, filename):
        geom_dict = load_crystfel_geometry(filename)
        modules = []
        for p in range(16):
            tiles = []
            modules.append(tiles)
            for a in range(8):
                d = geom_dict['panels']['p{}a{}'.format(p, a)]
                tiles.append(GeometryFragment.from_panel_dict(d))
        return cls(modules, filename=filename)

    def write_crystfel_geom(self, filename):
        from . import __version__

        panel_chunks = []
        for p, module in enumerate(self.modules):
            for a, fragment in enumerate(module):
                panel_chunks.append(fragment.to_crystfel_geom(p, a))

        with open(filename, 'w') as f:
            f.write(CRYSTFEL_HEADER_TEMPLATE.format(version=__version__))
            for chunk in panel_chunks:
                f.write(chunk)

        if self.filename == 'No file':
            self.filename = filename

    def inspect(self):
        """Plot the 2D layout of this detector geometry.

        Returns a matplotlib Figure object.
        """
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.collections import PatchCollection
        from matplotlib.figure import Figure
        from matplotlib.patches import Polygon

        fig = Figure((10, 10))
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(1, 1, 1)

        rects = []
        for p, module in enumerate(self.modules):
            for a, fragment in enumerate(module):
                corners = fragment.corners()[:, :2]  # Drop the Z dimension

                rects.append(Polygon(corners))

                if a in {0, 7}:
                    cx, cy, _ = fragment.centre()
                    ax.text(cx, cy, str(a),
                            verticalalignment='center',
                            horizontalalignment='center')
                elif a == 4:
                    cx, cy, _ = fragment.centre()
                    ax.text(cx, cy, 'p{}'.format(p),
                            verticalalignment='center',
                            horizontalalignment='center')

        pc = PatchCollection(rects, facecolor=(0.75, 1., 0.75), edgecolor=None)
        ax.add_collection(pc)

        ax.hlines(0, -100, +100, colors='0.75', linewidths=2)
        ax.vlines(0, -100, +100, colors='0.75', linewidths=2)

        ax.set_title('AGIPD-1M detector geometry ({})'.format(self.filename))
        return fig

    def compare(self, other, scale=1.):
        """Show a comparison of this geometry with another in a 2D plot.

        This shows the current geometry like :meth:`inspect`, with the addition
        of arrows showing how each panel is shifted in the other geometry.

        Parameters
        ----------

        other : AGIPD_1MGeometry
          A second geometry object to compare with this one.
        scale : float
          Scale the arrows showing the difference in positions.
          This is useful to show small differences clearly.
        """
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.collections import PatchCollection
        from matplotlib.figure import Figure
        from matplotlib.patches import Polygon, FancyArrow

        fig = Figure((10, 10))
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(1, 1, 1)

        rects = []
        arrows = []
        for p, module in enumerate(self.modules):
            for a, fragment in enumerate(module):
                corners = fragment.corners()[:, :2]  # Drop the Z dimension
                corner1, corner1_opp = corners[0], corners[2]

                rects.append(Polygon(corners))
                if a in {0, 7}:
                    cx, cy, _ = fragment.centre()
                    ax.text(cx, cy, str(a),
                            verticalalignment='center',
                            horizontalalignment='center')
                elif a == 4:
                    cx, cy, _ = fragment.centre()
                    ax.text(cx, cy, 'p{}'.format(p),
                            verticalalignment='center',
                            horizontalalignment='center')

                panel2 = other.modules[p][a]
                corners2 = panel2.corners()[:, :2]
                corner2, corner2_opp = corners2[0], corners2[2]
                dx, dy = corner2 - corner1
                if not (dx == dy == 0):
                    sx, sy = corner1
                    arrows.append(
                        FancyArrow(sx, sy, scale * dx, scale * dy, width=5,
                                   head_length=4))

                dx, dy = corner2_opp - corner1_opp
                if not (dx == dy == 0):
                    sx, sy = corner1_opp
                    arrows.append(
                        FancyArrow(sx, sy, scale * dx, scale * dy,
                                   width=5, head_length=5))

        pc = PatchCollection(rects, facecolor=(0.75, 1., 0.75),
                             edgecolor=None)
        ax.add_collection(pc)
        ac = PatchCollection(arrows)
        ax.add_collection(ac)

        # Set axis limits to fit all shapes, with some margin
        all_x = np.concatenate([s.xy[:, 0] for s in arrows + rects])
        all_y = np.concatenate([s.xy[:, 1] for s in arrows + rects])
        ax.set_xlim(all_x.min() - 20, all_x.max() + 20)
        ax.set_ylim(all_y.min() - 40, all_y.max() + 20)

        ax.set_title('Geometry comparison: {} → {}'
                     .format(self.filename, other.filename))
        ax.text(1, 0, 'Arrows scaled: {}×'.format(scale),
                horizontalalignment="right", verticalalignment="bottom",
                transform=ax.transAxes)
        return fig

    def position_modules_interpolate(self, data):
        """Assemble data from this detector according to where the pixels are.

        This performs interpolation, which is very slow.
        Use :meth:`position_modules_fast` to get a pixel-aligned approximation
        of the geometry.

        Parameters
        ----------

        data : ndarray
          The three dimensions should be channelno, pixel_ss, pixel_fs
          (lengths 16, 512, 128). ss/fs are slow-scan and fast-scan.

        Returns
        -------
        out : ndarray
          Array with the one dimension fewer than the input.
          The last two dimensions represent pixel y and x in the detector space.
        centre : ndarray
          (y, x) pixel location of the detector centre in this geometry.
        """
        assert data.shape == (16, 512, 128)
        size_yx, centre = self._get_dimensions()
        tmp = np.empty((16 * 8,) + size_yx, dtype=data.dtype)

        for i, (module, mod_data) in enumerate(zip(self.modules, data)):
            tiles_data = np.split(mod_data, 8)
            for j, (tile, tile_data) in enumerate(zip(module, tiles_data)):
                # We store (x, y, z), but numpy indexing, and hence affine_transform,
                # work like [y, x]. Rearrange the numbers:
                fs_vec_yx = tile.fs_vec[:2][::-1]
                ss_vec_yx = tile.ss_vec[:2][::-1]

                # Offset by centre to make all coordinates positive
                corner_pos_yx = tile.corner_pos[:2][::-1] + centre

                # Make the rotation matrix
                rotn = np.stack((ss_vec_yx, fs_vec_yx), axis=-1)

                # affine_transform takes a mapping from *output* to *input*.
                # So we reverse the forward transformation.
                transform = np.linalg.inv(rotn)
                offset = np.dot(rotn, corner_pos_yx)  # this seems to work, but is it right?

                affine_transform(tile_data, transform, offset=offset, cval=np.nan,
                                 output_shape=size_yx, output=tmp[i * 8 + j])

        # Silence warnings about nans - we expect gaps in the result
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            out = np.nanmax(tmp, axis=0)

        return out, centre

    def _get_dimensions(self):
        """Calculate appropriate array dimensions for assembling data.

        Returns (size_y, size_x), (centre_y, centre_x)
        """
        corners = []
        for module in self.modules:
            for tile in module:
                corners.append(tile.corners())
        corners = np.concatenate(corners)[:, :2]

        # Find extremes, add 1 px margin to allow for rounding errors
        min_xy = corners.min(axis=0).astype(int) - 1
        max_xy = corners.max(axis=0).astype(int) + 1

        size = max_xy - min_xy
        centre = -min_xy
        # Switch xy -> yx
        return tuple(size[::-1]), centre[::-1]

    @staticmethod
    def split_tiles(module_data):
        # Split into 8 tiles along the slow-scan axis
        return np.split(module_data, 8, axis=-2)

    def to_distortion_array(self):
        """Return distortion matrix for AGIPD detector, suitable for pyFAI

        Returns
        -------
        out: ndarray
            Dimension (8192=16(modules)*512(ss_dim), 128(fs_dim), 4, 3)
            type: float32
            4 is the number of corners of a pixel
            last dimension is for Z, Y, X location of each corner
        """
        distortion = np.zeros((8192, 128, 4, 3), dtype=np.float32)

        # Prepare some arrays to use inside the loop
        pixel_ss_index, pixel_fs_index = np.meshgrid(
            np.arange(0, 64), np.arange(0, 128), indexing='ij'
        )
        corner_ss_offsets = np.array([-.5, .5, .5, -.5])
        corner_fs_offsets = np.array([-.5, -.5, .5, .5])

        for m, mod in enumerate(self.modules, start=0):
            # module offset along first dimension of distortion array
            module_offset = m * 512

            for t, tile in enumerate(mod, start=0):
                corner_x, corner_y, corner_z = tile.corner_pos * self.pixel_size
                ss_unit_x, ss_unit_y, ss_unit_z = tile.ss_vec * self.pixel_size
                fs_unit_x, fs_unit_y, fs_unit_z = tile.fs_vec * self.pixel_size

                # Calculate coordinates of each pixel centre
                # 2D arrays, shape: (64, 128)
                pixel_centres_x = (
                    corner_x +
                    pixel_ss_index * ss_unit_x +
                    pixel_fs_index * fs_unit_x
                )
                pixel_centres_y = (
                    corner_y +
                    pixel_ss_index * ss_unit_y +
                    pixel_fs_index * fs_unit_y
                )
                pixel_centres_z = (
                    corner_z +
                    pixel_ss_index * ss_unit_z +
                    pixel_fs_index * fs_unit_z
                )

                # Calculate corner coordinates for each pixel
                # 3D arrays, shape: (64, 128, 4)
                corners_x = (
                    pixel_centres_x[:, :, np.newaxis] +
                    corner_ss_offsets * ss_unit_x +
                    corner_fs_offsets * fs_unit_x
                )
                corners_y = (
                    pixel_centres_y[:, :, np.newaxis] +
                    corner_ss_offsets * ss_unit_y +
                    corner_fs_offsets * fs_unit_y
                )
                corners_z = (
                    pixel_centres_z[:, :, np.newaxis] +
                    corner_ss_offsets * ss_unit_z +
                    corner_fs_offsets * fs_unit_z
                )

                # Which part of the array is this tile?
                tile_offset = module_offset + (t * 64)
                tile_slice = slice(tile_offset, tile_offset + tile.ss_pixels)

                # Insert the data into the array
                distortion[tile_slice, :, :, 0] = corners_z
                distortion[tile_slice, :, :, 1] = corners_y
                distortion[tile_slice, :, :, 2] = corners_x

        # Shift the x & y origin from the centre to the corner
        min_yx = distortion[..., 1:].min(axis=(0, 1, 2))
        distortion[..., 1:] -= min_yx

        return distortion


class SnappedGeometry:
    """Detector geometry approximated to align modules to a 2D grid

    The coordinates used in this class are (y, x) suitable for indexing a
    Numpy array; this does not match the (x, y, z) coordinates in the more
    precise geometry above.
    """
    def __init__(self, modules, geom: DetectorGeometryBase):
        self.modules = modules
        self.geom = geom

    def position_modules(self, data):
        """Implementation for position_modules_fast
        """
        assert data.shape[-3:] == self.geom.expected_data_shape
        size_yx, centre = self._get_dimensions()
        out = np.full(data.shape[:-3] + size_yx, np.nan, dtype=data.dtype)
        for i, module in enumerate(self.modules):
            mod_data = data[..., i, :, :]
            tiles_data = self.geom.split_tiles(mod_data)
            for j, tile in enumerate(module):
                tile_data = tiles_data[j]
                # Offset by centre to make all coordinates positive
                y, x = tile.corner_idx + centre
                h, w = tile.pixel_dims
                out[..., y:y+h, x:x+w] = tile.transform(tile_data)

        return out, centre

    def _get_dimensions(self):
        """Calculate appropriate array dimensions for assembling data.

        Returns (size_y, size_x), (centre_y, centre_x)
        """
        corners = []
        for module in self.modules:
            for tile in module:
                corners.append(tile.corner_idx)
                corners.append(tile.opp_corner_idx)
        corners = np.stack(corners)

        # Find extremes
        min_yx = corners.min(axis=0)
        max_yx = corners.max(axis=0)

        size = max_yx - min_yx
        centre = -min_yx
        return tuple(size), centre

    def plot_data(self, modules_data, axis_units='px'):
        """Implementation for plot_data_fast
        """
        from matplotlib.cm import viridis
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        if axis_units not in {'px', 'm'}:
            raise ValueError("axis_units must be 'px' or 'm', not {!r}"
                             .format(axis_units))

        fig = Figure((10, 10))
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(1, 1, 1)
        my_viridis = copy(viridis)
        # Use a dark grey for missing data
        my_viridis.set_bad('0.25', 1.)

        res, centre = self.position_modules(modules_data)
        min_y, min_x = -centre
        max_y, max_x = np.array(res.shape) - centre

        extent = np.array((min_x - 0.5, max_x + 0.5, min_y - 0.5, max_y + 0.5))
        cross_size = 20
        if axis_units == 'm':
            extent *= self.geom.pixel_size
            cross_size *= self.geom.pixel_size

        ax.imshow(res, origin='lower', cmap=my_viridis, extent=extent)
        ax.set_xlabel('metres' if axis_units == 'm' else 'pixels')
        ax.set_ylabel('metres' if axis_units == 'm' else 'pixels')

        # Draw a cross at the centre
        ax.hlines(0, -cross_size, +cross_size, colors='w', linewidths=1)
        ax.vlines(0, -cross_size, +cross_size, colors='w', linewidths=1)
        return fig


CRYSTFEL_HEADER_TEMPLATE = """\
; AGIPD-1M geometry file written by karabo_data {version}
; You may need to edit this file to add:
; - data and mask locations in the file
; - mask_good & mask_bad values to interpret the mask
; - adu_per_eV & photon_energy
; - clen (detector distance)
;
; See: http://www.desy.de/~twhite/crystfel/manual-crystfel_geometry.html

dim0 = %
res = 5000 ; 200 um pixels

rigid_group_q0 = p0a0,p0a1,p0a2,p0a3,p0a4,p0a5,p0a6,p0a7,p1a0,p1a1,p1a2,p1a3,p1a4,p1a5,p1a6,p1a7,p2a0,p2a1,p2a2,p2a3,p2a4,p2a5,p2a6,p2a7,p3a0,p3a1,p3a2,p3a3,p3a4,p3a5,p3a6,p3a7
rigid_group_q1 = p4a0,p4a1,p4a2,p4a3,p4a4,p4a5,p4a6,p4a7,p5a0,p5a1,p5a2,p5a3,p5a4,p5a5,p5a6,p5a7,p6a0,p6a1,p6a2,p6a3,p6a4,p6a5,p6a6,p6a7,p7a0,p7a1,p7a2,p7a3,p7a4,p7a5,p7a6,p7a7
rigid_group_q2 = p8a0,p8a1,p8a2,p8a3,p8a4,p8a5,p8a6,p8a7,p9a0,p9a1,p9a2,p9a3,p9a4,p9a5,p9a6,p9a7,p10a0,p10a1,p10a2,p10a3,p10a4,p10a5,p10a6,p10a7,p11a0,p11a1,p11a2,p11a3,p11a4,p11a5,p11a6,p11a7
rigid_group_q3 = p12a0,p12a1,p12a2,p12a3,p12a4,p12a5,p12a6,p12a7,p13a0,p13a1,p13a2,p13a3,p13a4,p13a5,p13a6,p13a7,p14a0,p14a1,p14a2,p14a3,p14a4,p14a5,p14a6,p14a7,p15a0,p15a1,p15a2,p15a3,p15a4,p15a5,p15a6,p15a7

rigid_group_p0 = p0a0,p0a1,p0a2,p0a3,p0a4,p0a5,p0a6,p0a7
rigid_group_p1 = p1a0,p1a1,p1a2,p1a3,p1a4,p1a5,p1a6,p1a7
rigid_group_p2 = p2a0,p2a1,p2a2,p2a3,p2a4,p2a5,p2a6,p2a7
rigid_group_p3 = p3a0,p3a1,p3a2,p3a3,p3a4,p3a5,p3a6,p3a7
rigid_group_p4 = p4a0,p4a1,p4a2,p4a3,p4a4,p4a5,p4a6,p4a7
rigid_group_p5 = p5a0,p5a1,p5a2,p5a3,p5a4,p5a5,p5a6,p5a7
rigid_group_p6 = p6a0,p6a1,p6a2,p6a3,p6a4,p6a5,p6a6,p6a7
rigid_group_p7 = p7a0,p7a1,p7a2,p7a3,p7a4,p7a5,p7a6,p7a7
rigid_group_p8 = p8a0,p8a1,p8a2,p8a3,p8a4,p8a5,p8a6,p8a7
rigid_group_p9 = p9a0,p9a1,p9a2,p9a3,p9a4,p9a5,p9a6,p9a7
rigid_group_p10 = p10a0,p10a1,p10a2,p10a3,p10a4,p10a5,p10a6,p10a7
rigid_group_p11 = p11a0,p11a1,p11a2,p11a3,p11a4,p11a5,p11a6,p11a7
rigid_group_p12 = p12a0,p12a1,p12a2,p12a3,p12a4,p12a5,p12a6,p12a7
rigid_group_p13 = p13a0,p13a1,p13a2,p13a3,p13a4,p13a5,p13a6,p13a7
rigid_group_p14 = p14a0,p14a1,p14a2,p14a3,p14a4,p14a5,p14a6,p14a7
rigid_group_p15 = p15a0,p15a1,p15a2,p15a3,p15a4,p15a5,p15a6,p15a7

rigid_group_collection_quadrants = q0,q1,q2,q3
rigid_group_collection_asics = p0,p1,p2,p3,p4,p5,p6,p7,p8,p9,p10,p11,p12,p13,p14,p15

"""



CRYSTFEL_PANEL_TEMPLATE = """
{name}/dim1 = {p}
{name}/dim2 = ss
{name}/dim3 = fs
{name}/min_fs = 0
{name}/min_ss = {min_ss}
{name}/max_fs = 127
{name}/max_ss = {max_ss}
{name}/fs = {fs_vec}
{name}/ss = {ss_vec}
{name}/corner_x = {corner_x}
{name}/corner_y = {corner_y}
{name}/coffset = {coffset}
"""

class LPD_1MGeometry(DetectorGeometryBase):
    """Detector layout for LPD-1M

    The coordinates used in this class are 3D (x, y, z), and represent multiples
    of the pixel size.
    """
    pixel_size = 5e-4  # 5e-4 metres == 0.5 mm
    frag_ss_pixels = 32
    frag_fs_pixels = 128
    expected_data_shape = (16, 256, 256)

    @classmethod
    def from_quad_positions(cls, quad_pos, asic_gap=4, panel_gap=4):
        """Generate an LPD-1M geometry from quadrant positions.

        This produces an idealised geometry, assuming all modules are perfectly
        flat, aligned and equally spaced within their quadrant.

        The quadrant positions are given in pixel units, referring to the
        corner of each quadrant where module 1, tile 1 is positioned.
        This is not the corner of the first pixel as the data is stored
        (the data starts in tile 8). In the initial detector layout, the corner
        positions are for the top-left corner of the quadrant, looking into
        the beam.
        """
        panels_across = [0, 0, 1, 1]
        panels_up = [0, -1, -1, 0]
        modules = []
        for p in range(16):
            quad = p // 4
            quad_corner_x, quad_corner_y = quad_pos[quad]

            p_in_quad = p % 4
            panel_corner_x = (quad_corner_x +
                  (panels_across[p_in_quad] * (256 + asic_gap + panel_gap)))
            panel_corner_y = (quad_corner_y +
                  (panels_up[p_in_quad] * (256 + (7 * asic_gap) + panel_gap)))

            tiles = []
            modules.append(tiles)

            for a in range(16):
                if a < 8:
                    up = -a
                    across = 0
                else:
                    up = -(15 - a)
                    across = 1

                corner_x = (panel_corner_x +
                            (cls.frag_fs_pixels + asic_gap) * across)
                # The first pixel read is the bottom left of the tile, whereas
                # our quad & panel corner coordinates are for the top left.
                # So there's an extra -32 pixel shift to correct it:
                corner_y = (panel_corner_y - cls.frag_ss_pixels +
                            ((cls.frag_ss_pixels + asic_gap) * up))

                tiles.append(GeometryFragment(
                    corner_pos=np.array([corner_x, corner_y, 0.]),
                    ss_vec=np.array([0, 1, 0]),
                    fs_vec=np.array([1, 0, 0]),
                    ss_pixels=cls.frag_ss_pixels,
                    fs_pixels=cls.frag_fs_pixels,
                ))
        return cls(modules)

    def inspect(self):
        """Plot the 2D layout of this detector geometry.

        Returns a matplotlib Figure object.
        """
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.collections import PatchCollection
        from matplotlib.figure import Figure
        from matplotlib.patches import Polygon

        fig = Figure((10, 10))
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(1, 1, 1)

        rects = []
        for p, module in enumerate(self.modules):
            for a, fragment in enumerate(module):
                corners = fragment.corners()[:, :2]  # Drop the Z dimension

                rects.append(Polygon(corners))

                if a in {7, 8, 15}:
                    cx, cy, _ = fragment.centre()
                    ax.text(cx, cy, str(a),
                            verticalalignment='center',
                            horizontalalignment='center')
                elif a == 0:
                    cx, cy, _ = fragment.centre()
                    ax.text(cx, cy, 'p{}'.format(p),
                            verticalalignment='center',
                            horizontalalignment='center')

        pc = PatchCollection(rects, facecolor=(0.75, 1., 0.75), edgecolor=None)
        ax.add_collection(pc)

        ax.hlines(0, -100, +100, colors='0.75', linewidths=2)
        ax.vlines(0, -100, +100, colors='0.75', linewidths=2)

        ax.set_title('LPD-1M detector geometry ({})'.format(self.filename))
        return fig

    @staticmethod
    def split_tiles(module_data):
        lhs, rhs = np.split(module_data, 2, axis=-1)
        # Tiles 1-8 (lhs here) are numbered top to bottom, whereas the array
        # starts at the bottom. So we reverse their order after splitting.
        return np.split(lhs, 8, axis=-2)[::-1] + np.split(rhs, 8, axis=-2)


if __name__ == '__main__':
    geom = AGIPD_1MGeometry.from_quad_positions(quad_pos=[
        (-525, 625),
        (-550, -10),
        (520, -160),
        (542.5, 475),
    ])
    geom.write_crystfel_geom('sample.geom')
