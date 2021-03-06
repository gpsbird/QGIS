# -*- coding: utf-8 -*-

"""
***************************************************************************
    vector.py
    ---------------------
    Date                 : February 2013
    Copyright            : (C) 2013 by Victor Olaya
    Email                : volayaf at gmail dot com
***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*                                                                         *
***************************************************************************
"""

__author__ = 'Victor Olaya'
__date__ = 'February 2013'
__copyright__ = '(C) 2013, Victor Olaya'

# This will get replaced with a git SHA1 when you do a git archive

__revision__ = '$Format:%H$'

import re
import os
import csv
import uuid

import psycopg2
from osgeo import ogr

from qgis.PyQt.QtCore import QVariant
from qgis.core import (QgsFields,
                       QgsField,
                       QgsGeometry,
                       QgsWkbTypes,
                       QgsVectorLayer,
                       QgsVectorFileWriter,
                       QgsDistanceArea,
                       QgsDataSourceUri,
                       QgsCredentials,
                       QgsFeatureRequest,
                       QgsSettings,
                       QgsPointXY,
                       QgsProcessingUtils)

from processing.tools import dataobjects


def resolveFieldIndex(source, attr):
    """This method takes an object and returns the index field it
    refers to in a layer. If the passed object is an integer, it
    returns the same integer value. If the passed value is not an
    integer, it returns the field whose name is the string
    representation of the passed object.

    Ir raises an exception if the int value is larger than the number
    of fields, or if the passed object does not correspond to any
    field.
    """
    if isinstance(attr, int):
        return attr
    else:
        index = source.fields().lookupField(attr)
        if index == -1:
            raise ValueError('Wrong field name')
        return index


def values(source, *attributes):
    """Returns the values in the attributes table of a feature source,
    for the passed fields.

    Field can be passed as field names or as zero-based field indices.
    Returns a dict of lists, with the passed field identifiers as keys.
    It considers the existing selection.

    It assummes fields are numeric or contain values that can be parsed
    to a number.
    """
    ret = {}
    indices = []
    attr_keys = {}
    for attr in attributes:
        index = resolveFieldIndex(source, attr)
        indices.append(index)
        attr_keys[index] = attr

    # use an optimised feature request
    request = QgsFeatureRequest().setSubsetOfAttributes(indices).setFlags(QgsFeatureRequest.NoGeometry)

    for feature in source.getFeatures(request):
        for i in indices:

            # convert attribute value to number
            try:
                v = float(feature.attributes()[i])
            except:
                v = None

            k = attr_keys[i]
            if k in ret:
                ret[k].append(v)
            else:
                ret[k] = [v]
    return ret


def testForUniqueness(fieldList1, fieldList2):
    '''Returns a modified version of fieldList2, removing naming
    collisions with fieldList1.'''
    changed = True
    while changed:
        changed = False
        for i in range(0, len(fieldList1)):
            for j in range(0, len(fieldList2)):
                if fieldList1[i].name() == fieldList2[j].name():
                    field = fieldList2[j]
                    name = createUniqueFieldName(field.name(), fieldList1)
                    fieldList2[j] = QgsField(name, field.type(), len=field.length(), prec=field.precision(), comment=field.comment())
                    changed = True
    return fieldList2


def createUniqueFieldName(fieldName, fieldList):
    def nextname(name):
        num = 1
        while True:
            returnname = '{name}_{num}'.format(name=name[:8], num=num)
            yield returnname
            num += 1

    def found(name):
        return any(f.name() == name for f in fieldList)

    shortName = fieldName[:10]

    if not fieldList:
        return shortName

    if not found(shortName):
        return shortName

    for newname in nextname(shortName):
        if not found(newname):
            return newname


def findOrCreateField(layer, fieldList, fieldName, fieldLen=24, fieldPrec=15):
    idx = layer.fields().lookupField(fieldName)
    if idx == -1:
        fn = createUniqueFieldName(fieldName, fieldList)
        field = QgsField(fn, QVariant.Double, '', fieldLen, fieldPrec)
        idx = len(fieldList)
        fieldList.append(field)

    return (idx, fieldList)


def extractPoints(geom):
    points = []
    if geom.type() == QgsWkbTypes.PointGeometry:
        if geom.isMultipart():
            points = geom.asMultiPoint()
        else:
            points.append(geom.asPoint())
    elif geom.type() == QgsWkbTypes.LineGeometry:
        if geom.isMultipart():
            lines = geom.asMultiPolyline()
            for line in lines:
                points.extend(line)
        else:
            points = geom.asPolyline()
    elif geom.type() == QgsWkbTypes.PolygonGeometry:
        if geom.isMultipart():
            polygons = geom.asMultiPolygon()
            for poly in polygons:
                for line in poly:
                    points.extend(line)
        else:
            polygon = geom.asPolygon()
            for line in polygon:
                points.extend(line)

    return points


def combineFields(fieldsA, fieldsB):
    """Create single field map from two input field maps.
    """
    fields = []
    fields.extend(fieldsA)
    namesA = [str(f.name()).lower() for f in fieldsA]
    for field in fieldsB:
        name = str(field.name()).lower()
        if name in namesA:
            idx = 2
            newName = name + '_' + str(idx)
            while newName in namesA:
                idx += 1
                newName = name + '_' + str(idx)
            field = QgsField(newName, field.type(), field.typeName())
        fields.append(field)

    real_fields = QgsFields()
    for f in fields:
        real_fields.append(f)
    return real_fields


def checkMinDistance(point, index, distance, points):
    """Check if distance from given point to all other points is greater
    than given value.
    """
    if distance == 0:
        return True

    neighbors = index.nearestNeighbor(point, 1)
    if len(neighbors) == 0:
        return True

    if neighbors[0] in points:
        np = points[neighbors[0]]
        if np.sqrDist(point) < (distance * distance):
            return False

    return True


def snapToPrecision(geom, precision):
    snapped = QgsGeometry(geom)
    if precision == 0.0:
        return snapped

    i = 0
    p = snapped.vertexAt(i)
    while p.x() != 0.0 and p.y() != 0.0:
        x = round(p.x() / precision, 0) * precision
        y = round(p.y() / precision, 0) * precision
        snapped.moveVertex(x, y, i)
        i = i + 1
        p = snapped.vertexAt(i)
    return QgsPointXY(snapped.x(), snapped.y())


def ogrConnectionString(uri):
    """Generates OGR connection sting from layer source
    """
    ogrstr = None

    context = dataobjects.createContext()
    layer = QgsProcessingUtils.mapLayerFromString(uri, context, False)
    if layer is None:
        return '"' + uri + '"'
    provider = layer.dataProvider().name()
    if provider == 'spatialite':
        # dbname='/geodata/osm_ch.sqlite' table="places" (Geometry) sql=
        regex = re.compile("dbname='(.+)'")
        r = regex.search(str(layer.source()))
        ogrstr = r.groups()[0]
    elif provider == 'postgres':
        # dbname='ktryjh_iuuqef' host=spacialdb.com port=9999
        # user='ktryjh_iuuqef' password='xyqwer' sslmode=disable
        # key='gid' estimatedmetadata=true srid=4326 type=MULTIPOLYGON
        # table="t4" (geom) sql=
        dsUri = QgsDataSourceUri(layer.dataProvider().dataSourceUri())
        conninfo = dsUri.connectionInfo()
        conn = None
        ok = False
        while not conn:
            try:
                conn = psycopg2.connect(dsUri.connectionInfo())
            except psycopg2.OperationalError:
                (ok, user, passwd) = QgsCredentials.instance().get(conninfo, dsUri.username(), dsUri.password())
                if not ok:
                    break

                dsUri.setUsername(user)
                dsUri.setPassword(passwd)

        if not conn:
            raise RuntimeError('Could not connect to PostgreSQL database - check connection info')

        if ok:
            QgsCredentials.instance().put(conninfo, user, passwd)

        ogrstr = "PG:%s" % dsUri.connectionInfo()
    elif provider == "oracle":
        # OCI:user/password@host:port/service:table
        dsUri = QgsDataSourceUri(layer.dataProvider().dataSourceUri())
        ogrstr = "OCI:"
        if dsUri.username() != "":
            ogrstr += dsUri.username()
            if dsUri.password() != "":
                ogrstr += "/" + dsUri.password()
            delim = "@"

        if dsUri.host() != "":
            ogrstr += delim + dsUri.host()
            delim = ""
            if dsUri.port() != "" and dsUri.port() != '1521':
                ogrstr += ":" + dsUri.port()
            ogrstr += "/"
            if dsUri.database() != "":
                ogrstr += dsUri.database()
        elif dsUri.database() != "":
            ogrstr += delim + dsUri.database()

        if ogrstr == "OCI:":
            raise RuntimeError('Invalid oracle data source - check connection info')

        ogrstr += ":"
        if dsUri.schema() != "":
            ogrstr += dsUri.schema() + "."

        ogrstr += dsUri.table()
    else:
        ogrstr = str(layer.source()).split("|")[0]

    return '"' + ogrstr + '"'


def ogrLayerName(uri):
    if os.path.isfile(uri):
        return os.path.basename(os.path.splitext(uri)[0])

    if ' table=' in uri:
        # table="schema"."table"
        re_table_schema = re.compile(' table="([^"]*)"\\."([^"]*)"')
        r = re_table_schema.search(uri)
        if r:
            return r.groups()[0] + '.' + r.groups()[1]
        # table="table"
        re_table = re.compile(' table="([^"]*)"')
        r = re_table.search(uri)
        if r:
            return r.groups()[0]
    elif 'layername' in uri:
        regex = re.compile('(layername=)([^|]*)')
        r = regex.search(uri)
        return r.groups()[1]

    fields = uri.split('|')
    basePath = fields[0]
    fields = fields[1:]
    layerid = 0
    for f in fields:
        if f.startswith('layername='):
            return f.split('=')[1]
        if f.startswith('layerid='):
            layerid = int(f.split('=')[1])

    ds = ogr.Open(basePath)
    if not ds:
        return None

    ly = ds.GetLayer(layerid)
    if not ly:
        return None

    name = ly.GetName()
    ds = None
    return name


NOGEOMETRY_EXTENSIONS = [
    u'csv',
    u'dbf',
    u'ods',
    u'xlsx',
]


class TableWriter(object):

    def __init__(self, fileName, encoding, fields):
        self.fileName = fileName
        if not self.fileName.lower().endswith('csv'):
            self.fileName += '.csv'

        self.encoding = encoding
        if self.encoding is None or encoding == 'System':
            self.encoding = 'utf-8'

        with open(self.fileName, 'w', newline='', encoding=self.encoding) as f:
            self.writer = csv.writer(f)
            if len(fields) != 0:
                self.writer.writerow(fields)

    def addRecord(self, values):
        with open(self.fileName, 'a', newline='', encoding=self.encoding) as f:
            self.writer = csv.writer(f)
            self.writer.writerow(values)

    def addRecords(self, records):
        with open(self.fileName, 'a', newline='', encoding=self.encoding) as f:
            self.writer = csv.writer(f)
            self.writer.writerows(records)
